"""Actual electrical AE acquisitions through immutable code-domain ISP replay."""

import hashlib
import io
import json
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from test_electrical_control_workflow import workflow as _electrical_workflow
from test_hdr_workflow import finish, load_array
from test_hdr_workflow import workflow as _hdr_workflow
from test_retained_charge_workflow import workflow as _retained_workflow

from camera_platform import nonlinear_examples, retained_isp
from camera_platform import retained_isp_replay as replay
from camera_platform.registry import canonical
from camera_platform.service import PlatformService

# Explicit fixture dependencies remain visible when this file runs on its own.
hdr_workflow = _hdr_workflow
retained_workflow = _retained_workflow
workflow = _electrical_workflow


def acquire(workflow, *, nonlinear=False, pwm=False):
    service, sdk, assembly, scene, control, parent, _, request = workflow
    if nonlinear or pwm:
        draft = sdk.template(
            "capture-program",
            assembly_id=assembly["id"],
            mode="electrical_control",
            control_sequence_id=control["id"],
        )
        draft.update(revision="isp-nonlinear-pwm", parents=[parent["id"]])
        draft["payload"]["electrical_control_policy"] = deepcopy(
            parent["payload"]["electrical_control_policy"]
        )
        if nonlinear:
            draft["payload"]["electrical_model"] = nonlinear_examples.single_model(
                parent["sensor_model_snapshot"]["payload"]["readout"]
            )
        if pwm:
            draft["payload"]["waveform"] = {
                "schema": "camera_light_waveform_v1",
                "kind": "pwm",
                "clock_id": control["payload"]["clock"]["clock_id"],
                "period_ns": 731_000,
                "on_time_ns": 173_000,
                "phase_origin_ns": 317_000,
                "low": 0,
                "high": 1,
            }
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
    return service, sdk, finish(service, service.submit_run(request))


def replay_request(service, sdk, run, *, active=True):
    draft = sdk.retained_isp_template(run["id"], "single")
    draft.update(name="Actual AE code processing", revision="electrical-isp")
    if active:
        draft["payload"]["spatial_filter"].update(
            mode="same_site_bilateral_code_v1",
            strength=0.5,
        )
        draft["payload"]["temporal_filter"].update(
            mode="causal_difference_gate_v1",
            previous_weight=0.5,
            threshold_dn=1_000_000,
        )
    model = service.create_model(draft)
    return {
        "schema": replay.SCHEMA,
        "name": "Actual AE retained-code replay",
        "source_kind": "run",
        "source_id": run["id"],
        "isp_model_id": model["id"],
        "selector": {"schema": "camera_retained_raw_selection_v1", "readout_id": "single"},
    }


@pytest.mark.parametrize("nonlinear,pwm", [(False, False), (True, True)])
def test_actual_ae_raw_and_conditions_drive_code_isp_without_exposure_normalization(
    workflow,
    nonlinear,
    pwm,
    monkeypatch,
):
    service, sdk, source = acquire(workflow, nonlinear=nonlinear, pwm=pwm)
    original = deepcopy(source)
    acquisition = source["result"]["summary"]
    expected = load_array(service, source, "raw_groups")[:, 0]
    exposures = [f["applied"]["exposure_ns"] for f in acquisition["control_sequence"]["frames"]]
    assert len(set(exposures)) > 1
    choice = next(c for c in sdk.retained_isp_sources() if c["source_id"] == source["id"])
    assert choice["source_plugin_id"] == "electrical_control_v1"
    assert choice["acquisition_mode"] == "closed_loop"
    assert choice["actual_frame_conditions"] == acquisition["frame_conditions"]
    assert choice["integrity"] == "verified_on_selection"

    process, calls = retained_isp.process_raw, []

    def isolated(raw, raw_format, profile, **kwargs):
        np.testing.assert_array_equal(raw, expected)
        assert set(kwargs) == {"geometry", "frame_records", "events"}
        assert "control_trace" not in kwargs and "planning_contract" not in kwargs
        assert not any(key in canonical(kwargs) for key in ("residual_e", "delivered_e", "ledger"))
        assert [f["applied"]["exposure_ns"] for f in kwargs["frame_records"]] == exposures
        assert kwargs["events"] == acquisition["retained_charge"]["events"]
        calls.append(raw.copy())
        return process(raw, raw_format, profile, **kwargs)

    monkeypatch.setattr(retained_isp, "process_raw", isolated)
    result = finish(service, sdk.post("replays", replay_request(service, sdk, source)))
    assert len(calls) == 1
    summary = result["result"]["summary"]
    np.testing.assert_array_equal(load_array(service, result, "raw_frames"), expected)
    assert summary["raw_format"] == acquisition["raw_formats"][0]
    assert summary["raw_format"]["response_type"] == (
        "nonlinear_charge_voltage" if nonlinear else "linear"
    )
    assert summary["acquisition_mode"] == "closed_loop"
    assert summary["control_trace"] == acquisition["electrical_control"]
    assert summary["realized_acquisition"] == acquisition["prediction"]["sample_geometry"]
    assert summary["planning_contract"] == acquisition["prediction"]["sample_geometry"]["plan"]
    assert summary["control_trace_role"] == "evidence_only_not_processing_inputs"
    assert [f["exposure_ns"] for f in summary["frame_conditions"]] == exposures
    assert [f["applied"]["exposure_ns"] for f in summary["frames"]] == exposures
    assert not summary["source_truth_arrays_used"] and not summary["charge_inversion"]
    assert not summary["dark_subtraction"]
    np.testing.assert_array_equal(
        load_array(service, result, "black_relative_dn_frames"),
        expected.astype(float) - summary["raw_format"]["black_level_dn"],
    )
    spatial = load_array(service, result, "spatial_dn_frames")
    temporal = load_array(service, result, "temporal_dn_frames")
    reasons = load_array(service, result, "temporal_reset_reason_frames")
    gates = load_array(service, result, "temporal_gate_mask_frames")
    for i in range(1, len(exposures)):
        if exposures[i] != exposures[i - 1]:
            assert np.all(reasons[i] & retained_isp.RESET_REASONS["settings"])
            assert not gates[i].any()
            np.testing.assert_array_equal(temporal[i], spatial[i])
    assert np.max(np.abs(spatial - load_array(service, result, "black_relative_dn_frames"))) > 0
    assert np.isfinite(load_array(service, result, "rgb_frames")).all()
    assert service.registry.get_run(source["id"]) == original
    composite = sdk.get("retained-isp/workbench")
    assert next(s for s in composite["sources"] if s["source_id"] == source["id"]) == choice
    assert next(r for r in composite["replays"] if r["id"] == result["id"])["status"] == "succeeded"


def test_actual_ae_cache_force_restart_and_bundle_keep_original_raw_and_trace(workflow):
    service, sdk, source = acquire(workflow)
    request = replay_request(service, sdk, source, active=False)
    result = finish(service, sdk.post("replays", request))
    assert sdk.post("replays", request)["id"] == result["id"]
    forced = finish(service, sdk.post("replays", {**request, "force": True}))
    assert forced["id"] != result["id"]
    for name in ("raw_frames", "temporal_dn_frames", "rgb_frames"):
        np.testing.assert_array_equal(
            load_array(service, forced, name), load_array(service, result, name)
        )
    np.testing.assert_allclose(
        load_array(service, result, "rgb_frames"),
        load_array(service, source, "rgb_groups")[:, 0],
        rtol=0,
        atol=1e-14,
    )
    cold = PlatformService(service.registry.directory)
    assert replay.verify(cold, cold.registry.get_run(result["id"])) == result
    bundle = sdk.replay_bundle(result["id"])
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["source"] == source
        assert (
            manifest["replay"]["source"]["control_trace"]
            == source["result"]["summary"]["electrical_control"]
        )
        for record in (source, result):
            for artifact in record["artifacts"]:
                saved = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
                assert hashlib.sha256(saved).hexdigest() == artifact["sha256"]
    assert len(bundle) <= result["evidence_forecast"]["archive_bytes_bound"]
    assert service.registry.audit()["integrity"] == "verified"


def test_discovery_keeps_legacy_retained_entries_unchanged_and_refuses_paired_ae():
    class MetadataOnly:
        def list_simulation_runs(self):
            return records

    artifact = {"name": "raw_groups.npy", "shape": [2, 1, 8, 10], "sha256": "a" * 64}
    summary = {"readout_ids": ["single"], "raw_formats": [{"response_type": "nonlinear"}]}
    legacy = {
        "id": "legacy",
        "name": "Retained",
        "plugin_id": "retained_charge_v1",
        "status": "succeeded",
        "artifacts": [artifact],
        "result": {"summary": summary},
    }
    records = [legacy]
    before = replay.choices(MetadataOnly())
    records = [
        legacy,
        {
            **deepcopy(legacy),
            "id": "queued",
            "plugin_id": "electrical_control_v1",
            "status": "queued",
        },
        {
            **deepcopy(legacy),
            "id": "paired",
            "plugin_id": "electrical_control_v1",
            "artifacts": [{**artifact, "shape": [2, 2, 8, 10]}],
            "result": {"summary": {"readout_ids": ["HCG", "LCG"], "raw_formats": [{}, {}]}},
        },
    ]
    assert replay.choices(MetadataOnly()) == before
    assert "acquisition_mode" not in before[0]
    assert "actual_frame_conditions" not in before[0]
