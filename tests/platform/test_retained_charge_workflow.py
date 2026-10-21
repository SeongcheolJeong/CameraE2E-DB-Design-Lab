"""API, immutable queue/cache, delivered-source refusal and portable evidence."""

import hashlib
import json
import zipfile
from copy import deepcopy

import pytest
from test_hdr_workflow import finish, load_array
from test_hdr_workflow import workflow as _hdr_workflow

from camera_platform import imaging, nonlinear_examples, sequences
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.evidence import build_bundle
from camera_platform.retained_charge_runtime import METRIC_ID, OBSERVATION
from camera_platform.validation import compare

hdr_workflow = _hdr_workflow


@pytest.fixture
def workflow(hdr_workflow):
    service, sdk, base, *_ = hdr_workflow
    sensor = next(
        service.registry.get("models", i)
        for i in base["model_ids"]
        if service.registry.get("models", i)["kind"] == "sensor"
    )
    payload = {k: deepcopy(v) for k, v in sensor.items() if k in ModelCreate.model_fields}
    payload.update(
        name="Retained full-native sensor", revision="retained-16", parents=[sensor["id"]]
    )
    for key in ("active_width", "active_height"):
        payload["properties"][key] = {
            "value": payload["payload"]["pixel_pitch_um"] * 16 / 1000,
            "unit": "mm",
        }
    small = service.create_model(payload)
    fields = {k: deepcopy(v) for k, v in base.items() if k in AssemblyCreate.model_fields}
    fields.update(
        name="Retained complete native camera",
        revision="retained-16",
        model_ids=[small["id"] if i == sensor["id"] else i for i in base["model_ids"]],
    )
    assembly = service.create_assembly(fields)
    scene = sequences.create_fixture(
        service,
        sequences.fixture_template(service, assembly["id"])
        | {"shape": [16, 16], "knot_count": 2, "pattern": "static"},
    )
    control = sdk.create_control_sequence(
        sequences.control_template(service, assembly["id"], scene["id"])
    )
    draft = sdk.template(
        "capture-program",
        assembly_id=assembly["id"],
        mode="retained_charge",
        control_sequence_id=control["id"],
    )
    draft["payload"]["retained_charge_model"]["transfer_efficiency"] = [0.5] * 4
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
    request = {
        "assembly_id": assembly["id"],
        "test_id": test["id"],
        "scene_sequence_id": scene["id"],
        "control_sequence_id": control["id"],
        "capture_program_id": program["id"],
        "plugin_id": "retained_charge_v1",
        "parameters": {"seed": 591, "noise_enabled": False},
    }
    return service, sdk, assembly, scene, control, program, test, request


def test_sdk_run_freezes_whole_sequence_exports_exact_evidence_and_refuses_inverse_replay(workflow):
    service, sdk, assembly, scene, control, program, test, request = workflow
    queued = sdk.submit_retained_charge(
        assembly["id"],
        test["id"],
        scene["id"],
        control["id"],
        program["id"],
        seed=591,
        noise_enabled=False,
    )
    events = queued["request"]["retained_charge_events"]
    assert events == test["stimulus"]["sample_contract"]["events"]
    run = finish(service, queued)
    s = run["result"]["summary"]
    assert sdk.get("capture_programs/" + program["id"]) == program
    assert service.submit_run(request)["id"] == run["id"]
    assert s["prediction"]["metric_id"] == METRIC_ID
    assert s["prediction"]["sample_geometry"] == test["stimulus"]["sample_contract"]
    assert s["retained_charge"]["events"] == events
    raw = load_array(service, run, "raw_groups")
    assert raw.shape == (4, 1, 16, 16)
    assert s["preview_contract"]["charge_inversion"] is False
    assert all(
        service.registry.blob_path(f["preview_artifact_id"]).read_bytes().startswith(b"\x89PNG")
        for f in s["frames"]
    )
    with pytest.raises(ValueError, match="[Rr]etained|[Dd]elivered"):
        imaging.replay_source(service, "run", run["id"])
    assert not any(r["source_id"] == run["id"] for r in sdk.get("replay-sources"))
    from camera_platform.isp_charge_source import resolve

    with pytest.raises(ValueError, match="[Rr]etained|[Dd]elivered"):
        resolve(
            service,
            "run",
            run["id"],
            {"schema": "camera_nonlinear_raw_selection_v1", "mode": "sequential_frames"},
        )
    exported = service.export_run(run["id"])
    assert "capture_program_id" in exported["replay"] and "zero" in exported["replay"]
    with zipfile.ZipFile(build_bundle(service, "runs", run["id"])) as archive:
        for artifact in run["artifacts"]:
            content = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
            assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["run"]["request"]["retained_charge_events"] == events
    assert service.registry.audit()["integrity"] == "verified"
    assert not service.registry.list("measurements")


@pytest.mark.parametrize("family", ["nonlinear", "paired", "paired_nonlinear"])
def test_saved_electrical_families_run_without_legacy_calibration_and_keep_domains(
    workflow, family
):
    from camera_platform import dcg_electrical

    service, sdk, assembly, scene, control, parent, _, request = workflow
    sensor = parent["sensor_model_snapshot"]["payload"]["readout"]
    models = {
        "nonlinear": nonlinear_examples.single_model,
        "paired": dcg_electrical.default_model,
        "paired_nonlinear": nonlinear_examples.shared_model,
    }
    draft = sdk.template(
        "capture-program",
        assembly_id=assembly["id"],
        mode="retained_charge",
        control_sequence_id=control["id"],
    )
    draft.update(revision=family, parents=[parent["id"]])
    draft["payload"]["electrical_model"] = models[family](sensor)
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
    run = finish(
        service,
        service.submit_run({**request, "capture_program_id": program["id"], "test_id": test["id"]}),
    )
    summary = run["result"]["summary"]
    assert summary["prediction"]["valid_domain"]["temperature_c"] == {
        "min": 25,
        "max": 25,
        "unit": "degC",
    }
    assert len(summary["raw_formats"]) == (1 if family == "nonlinear" else 2)
    assert "calibrations" not in summary
    assert summary["prediction"]["method_and_version"] == OBSERVATION
    assert summary["capture_program_id"] == program["id"]


@pytest.mark.parametrize("field", ["events", "events_type", "noise", "program", "control"])
def test_frozen_request_tampering_refused_before_acquisition(workflow, field):
    service, _, _, _, _, _, _, request = workflow
    queued = service.submit_run(request)
    altered = deepcopy(queued)
    if field == "events":
        altered["request"]["retained_charge_events"]["frames"][0]["row_open_ns"][0] += 1
    elif field == "events_type":
        altered["request"]["retained_charge_events"]["frames"][0]["row_gap_ns"][0] = False
    elif field == "noise":
        altered["request"]["noise_enabled"] = True
    elif field == "program":
        altered["request"]["capture_program"]["pd_clear_before_exposure"][1]["clear"] = True
    else:
        altered["request"]["control_sequence"]["frames"][0]["applied"]["exposure_ns"] += 1
    with pytest.raises(ValueError, match="fixed source evidence"):
        service.execute_run(altered)


def test_delivered_sample_contract_comparison_and_old_estimator_refusal(workflow):
    service, _, _, _, _, _, test, request = workflow
    run = finish(service, service.submit_run(request))
    prediction = deepcopy(run["result"]["summary"]["prediction"])
    reference = deepcopy(prediction)
    reference.update(
        id="independent-simulated-delivered", dataset_ids=["independent-simulated-delivered"]
    )
    report = compare(prediction, reference, test)
    assert {issue["code"] for issue in report["issues"]} == {"missing_uncertainty"}
    assert not report["physical_validation_pass"]
    # A local comparison fixture exercises the operator with explicit synthetic
    # bounds; the saved runtime evidence continues to declare unknown uncertainty.
    for record in (prediction, reference):
        record["uncertainty"] = {"kind": "absolute_bound", "value": 0, "unit": "DN"}
    assert compare(prediction, reference, test)["comparison_valid"]
    reference["method_and_version"] = "sampled_raw_dark_corrected_v1"
    report = compare(prediction, reference, test)
    assert not report["comparison_valid"]
    assert "retained_sample_contract_mismatch" in {issue["code"] for issue in report["issues"]}
    reference = deepcopy(prediction)
    reference["sample_geometry"]["events"]["frames"][0]["row_transfer_ns"][0] += 1
    assert not compare(prediction, reference, test)["comparison_valid"]


def test_noise_flag_is_strict_new_runtime_only_and_changes_cache_identity(workflow):
    service, _, _, _, _, _, _, request = workflow
    first = service.submit_run(request)
    second = service.submit_run({**request, "parameters": {"seed": 591, "noise_enabled": True}})
    assert first["id"] != second["id"]
    with pytest.raises(ValueError, match="boolean"):
        service.submit_run({**request, "parameters": {"seed": 591, "noise_enabled": 1}})


@pytest.mark.parametrize(
    "artifact_name", ["capture-program-source.json", "capture-program-normalized.json"]
)
@pytest.mark.parametrize("stage", ["cache", "queued"])
def test_corrupt_program_evidence_refused_even_for_cache_or_queued_worker(
    workflow, artifact_name, stage, monkeypatch
):
    from camera_platform.registry import IntegrityError
    from camera_platform.retained_charge_runtime import RetainedChargeRuntime
    from camera_platform.worker import execute

    service, _, _, _, _, program, _, request = workflow
    queued = service.submit_run(request)
    completed = finish(service, queued) if stage == "cache" else None
    artifact = next(a for a in program["artifacts"] if a["name"] == artifact_name)
    service.registry.blob_path(artifact["sha256"]).write_bytes(b"owned fixture corruption")
    monkeypatch.setattr(
        RetainedChargeRuntime, "execute", lambda *a, **k: pytest.fail("Corrupt source reached RAW")
    )
    if stage == "cache":
        with pytest.raises(IntegrityError, match="checksum"):
            service.submit_run(request)
        assert service.registry.get_run(completed["id"]) == completed
    else:
        claimed = service.registry.claim()
        assert claimed["id"] == queued["id"]
        execute(service, claimed["id"])
        failed = service.registry.get_run(claimed["id"])
        assert failed["status"] == "failed" and failed["result"] is None
        assert "checksum" in failed["error"]


@pytest.mark.parametrize(
    "field", ["event_hash", "event_clock", "crop", "huge_time", "history_schema", "metric"]
)
def test_mutually_agreed_malformed_delivered_contract_is_not_comparable(workflow, field):
    service, _, _, _, _, _, test, request = workflow
    run = finish(service, service.submit_run(request))
    prediction = deepcopy(run["result"]["summary"]["prediction"])
    test = deepcopy(test)
    contract = test["stimulus"]["sample_contract"]
    if field == "event_hash":
        contract["events"]["schedule_sha256"] = "a" * 64
    elif field == "event_clock":
        contract["events"]["clock"]["clock_id"] = "other"
    elif field == "crop":
        contract["sampling"]["crop_origin"] = [1, 1]
    elif field == "huge_time":
        contract["events"]["frames"][0]["row_transfer_ns"][0] = 10**1000
    elif field == "history_schema":
        contract["topology"] = "independent_reset"
    else:
        test["metric_id"] = "raw.channel_mean_dn_sequence"
    prediction["sample_geometry"] = deepcopy(contract)
    reference = deepcopy(prediction)
    report = compare(prediction, reference, test)
    assert not report["comparison_valid"]
    assert any(
        issue["code"] in {"invalid_retained_sample_contract", "retained_observation_mismatch"}
        for issue in report["issues"]
    )
