"""Matched PWM gain screening uses completed original RAW and rejects drift."""

from copy import deepcopy

import numpy as np
import pytest
from test_hdr_workflow import finish, load_array
from test_hdr_workflow import workflow as hdr_workflow
from test_retained_charge_workflow import workflow

from camera_platform import factory, lfm_gain_screen, sequences
from camera_platform.composer import check_assembly
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.registry import IntegrityError


def _candidate_runs(workflow, *, declare_gain_roles=True):
    service, sdk, prior_assembly, _, _, *_ = workflow
    model_ids = []
    for identity in prior_assembly["model_ids"]:
        original = service.registry.get("models", identity)
        if original["kind"] == "lens":
            parent = service.registry.get("models", original["parents"][0])
            base_fields = {key: deepcopy(value) for key, value in parent.items()
                           if key in ModelCreate.model_fields}
            base_fields.update(name=parent["name"] + " synthetic gain range",
                               revision="synthetic-gain-0.4-to-1.0", parents=[parent["id"]])
            base_fields["valid_domain"]["gain"] = {"min": 0.4, "max": 1.0, "unit": "1"}
            if declare_gain_roles:
                base_fields["properties"]["analog_gain_applicability"] = {
                    "schema": "camera_analog_gain_applicability_v1",
                    "role": "optical_response_independent",
                    "basis": "authored_model_scope",
                }
            wider_base = service.create_model(base_fields)
            model_ids.append(factory.publish(service, {
                "job_id": original["psf_factory_job_id"],
                "base_lens_model_id": wider_base["id"],
                "name": original["name"] + " synthetic gain range",
                "revision": "synthetic-gain-0.4-to-1.0",
                "conditions": original["factory_application_conditions"],
                "condition_scope": original["factory_condition_scope"],
            })["id"])
            continue
        fields = {key: deepcopy(value) for key, value in original.items()
                  if key in ModelCreate.model_fields}
        fields.update(name=original["name"] + " synthetic gain range",
                      revision="synthetic-gain-0.4-to-1.0", parents=[identity])
        fields["valid_domain"]["gain"] = {"min": 0.4, "max": 1.0, "unit": "1"}
        if declare_gain_roles:
            fields["properties"]["analog_gain_applicability"] = {
                "schema": "camera_analog_gain_applicability_v1",
                "role": ("sensor_model_readout_condition" if original["kind"] == "sensor"
                         else "raw_input_processing_condition"),
                "basis": "authored_model_scope",
            }
        model_ids.append(service.create_model(fields)["id"])
    assembly_fields = {key: deepcopy(value) for key, value in prior_assembly.items()
                       if key in AssemblyCreate.model_fields}
    assembly_fields.update(name="Synthetic matched gain camera", revision="gain-0.4-to-1.0",
                           model_ids=model_ids)
    assembly = service.create_assembly(assembly_fields)
    scene = sequences.create_fixture(
        service, sequences.fixture_template(service, assembly["id"])
        | {"shape": [16, 16], "knot_count": 2, "pattern": "static"},
    )
    source = sequences.control_template(service, assembly["id"], scene["id"])
    source.update(name="Matched PWM gain control", revision="gain-1")
    for frame in source["payload"]["frames"]:
        frame["applied"]["exposure_ns"] = 8_000_000
        frame["applied"]["gain"] = 1.0
    baseline = sdk.create_control_sequence(source)
    draft = sdk.template("capture-program", assembly_id=assembly["id"],
                         mode="electrical_control_trapped_pwm_period",
                         control_sequence_id=baseline["id"])
    draft["payload"]["electrical_control_policy"]["target_black_relative_dn"] = {
        "minimum": 200.0, "maximum": 300.0,
    }
    draft["payload"]["waveform"]["period_ns"] = 8_000_000
    draft["payload"]["waveform"]["on_time_ns"] = 2_000_000
    draft["payload"]["electrical_control_policy"]["period_lattice"]["period_ns"] = 8_000_000
    draft["payload"]["electrical_control_policy"]["exposure_grid_ns"] = {
        "minimum": 8_000_000, "maximum": 8_000_000, "step": 8_000_000,
    }
    program = sdk.create_capture_program(draft)
    runs = []
    for gain in ((1.0, 0.5, 0.4) if declare_gain_roles else (1.0,)):
        fields = deepcopy(source)
        fields.update(name=f"Matched PWM gain {gain}", revision=f"gain-{gain}")
        for frame in fields["payload"]["frames"]:
            frame["applied"]["gain"] = gain
        selected = baseline if gain == 1.0 else sdk.create_control_sequence(fields)
        test = service.create_test(sdk.template(
            "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
            control_sequence_id=selected["id"], capture_program_id=program["id"],
        ))
        run = finish(service, service.submit_run({
            "assembly_id": assembly["id"], "scene_sequence_id": scene["id"],
            "control_sequence_id": selected["id"], "capture_program_id": program["id"],
            "test_id": test["id"], "plugin_id": "electrical_control_v1",
            "parameters": {"seed": 713, "noise_enabled": False},
        }))
        runs.append(run)
    return service, sdk, runs


def test_gain_screen_http_persistence_original_raw_and_match(workflow):
    service, sdk, runs = _candidate_runs(workflow)
    spec = {"schema": lfm_gain_screen.REQUEST_SCHEMA, "name": "Matched PWM gain screen",
            "run_ids": [run["id"] for run in runs]}
    response = sdk.http.post("/api/v1/lfm/gain-screens", json=spec)
    assert response.status_code == 201, response.text
    record = response.json()
    result = record["analysis"]
    assert result["candidate_count"] == 3
    assert [c["gain"] for c in result["candidates"]] == [0.4, 0.5, 1.0]
    assert result["automatic_selection"] is False
    assert result["physical_validation"] == "not_established"
    assert result["charge_or_trap_truth_used"] is False
    assert result["permitted_exposure_ns"] == 8_000_000
    assert result["observed_pareto_run_ids"]
    assert sdk.http.get("/api/v1/lfm/gain-screens/" + record["id"]).json() == record
    assert sdk.http.get("/api/v1/lfm/gain-screens").json() == [record]
    assert lfm_gain_screen.submit(service, spec)["id"] == record["id"]
    baseline_raw = load_array(service, runs[0], "raw_groups")
    raw_format = runs[0]["result"]["summary"]["raw_formats"][0]
    black = raw_format["black_level_dn"]
    white = 2 ** raw_format["bit_depth"] - 1
    # Independent numerical expectation, not just equality of two summaries:
    # in this noise-free, unclipped linear fixture, a changed analog gain must
    # alter actual RAW by the expected factor within the two ADC roundings.
    assert np.all((baseline_raw > black) & (baseline_raw < white))
    for run in runs:
        candidate = next(c for c in result["candidates"] if c["run_id"] == run["id"])
        raw = load_array(service, run, "raw_groups")
        gain = candidate["gain"]
        expected = gain * (baseline_raw.astype(float) - black)
        assert np.max(np.abs((raw.astype(float) - black) - expected)) <= (1 + gain) / 2 + 1e-9
        if gain != 1:
            assert not np.array_equal(raw, baseline_raw)
        assert raw.shape[0] == len(candidate["frames"])
        assert [frame["observed_dn"] for frame in candidate["frames"]] == [
            frame["statistics"]["value_dn"]
            for frame in run["result"]["summary"]["electrical_control"]["frames"]
        ]
        assert all(frame["applied_exposure_ns"] == 8_000_000
                   for frame in candidate["frames"])


def test_gain_screen_rejects_repeated_gain_and_tampered_original_raw(workflow):
    service, _, runs = _candidate_runs(workflow)
    spec = {"schema": lfm_gain_screen.REQUEST_SCHEMA, "name": "Matched PWM gain screen",
            "run_ids": [run["id"] for run in runs]}
    before = len(service.registry.list(lfm_gain_screen.KIND))
    with pytest.raises(ValueError, match="distinct completed run IDs"):
        lfm_gain_screen.submit(service, spec | {"run_ids": [runs[0]["id"], runs[0]["id"]]})
    assert len(service.registry.list(lfm_gain_screen.KIND)) == before
    record = lfm_gain_screen.submit(service, spec)
    artifact = next(a for a in runs[0]["artifacts"] if a["name"] == "raw_groups.npy")
    service.registry.blob_path(artifact["sha256"]).write_bytes(b"tampered original RAW")
    with pytest.raises(IntegrityError, match="RAW|checksum"):
        lfm_gain_screen.get(service, record["id"])


def test_original_fixture_declares_no_alternative_gain(workflow):
    service, _, assembly, *_ = workflow
    models = [service.registry.get("models", identity) for identity in assembly["model_ids"]]
    assert all(model["valid_domain"]["gain"] == {"min": 1.0, "max": 1.0, "unit": "1"}
               for model in models)
    proposal = deepcopy(assembly)
    proposal["conditions"]["gain"] = {"value": 0.5, "unit": "1"}
    assert check_assembly(proposal, models)["status"] != "PASS"
