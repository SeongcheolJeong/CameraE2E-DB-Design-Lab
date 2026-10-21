"""Actual AE code-ISP studies retain acquired timing, not the initial scaffold."""

import hashlib
import json
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from test_electrical_control_workflow import workflow as workflow
from test_hdr_workflow import finish, load_array
from test_hdr_workflow import workflow as _hdr_workflow
from test_retained_charge_workflow import workflow as _retained_workflow
from test_retained_isp_comparison import specification
from test_retained_isp_workflow import payload

from camera_platform import retained_isp_comparison as comparison
from camera_platform.registry import IntegrityError, canonical

hdr_workflow = _hdr_workflow
retained_workflow = _retained_workflow


def prepared(workflow):
    service, sdk, *_, request = workflow
    source = finish(service, service.submit_run(request))
    replay_request = payload(service, sdk, source, enabled=True)
    spec = specification(
        source_id=source["id"], isp_model_id=replay_request["isp_model_id"],
    )
    return service, sdk, source, spec


def test_actual_ae_study_compares_same_raw_and_preserves_all_acquisition_evidence(workflow):
    service, sdk, source, spec = prepared(workflow)
    planned = comparison.plan(service, spec)
    queued = comparison.submit(service, spec, planned["plan_hash"])
    completed = finish(service, queued)
    saved = comparison.get(service, completed["id"])
    summary = saved["result"]["summary"]
    actual = source["result"]["summary"]
    assert summary["acquisition_mode"] == "closed_loop"
    assert summary["planning_contract"] == actual["planning_contract"]
    assert summary["realized_acquisition"] == actual["prediction"]["sample_geometry"]
    assert summary["control_trace"] == actual["electrical_control"]
    assert summary["retained_charge_events"] == actual["retained_charge"]["events"]
    assert [f["applied"] for f in summary["frame_records"]] == [
        f["applied"] for f in actual["control_sequence"]["frames"]
    ]
    acquired = [f["exposure_ns"] for f in summary["frame_conditions"]]
    initial = [
        f["applied"]["exposure_ns"]
        for f in source["control_sequence_snapshot"]["payload"]["frames"]
    ]
    assert acquired != initial
    assert summary["physical_validation"] == "not_established"
    assert summary["source_snapshot_role"] == "evidence_only_not_processing_inputs"
    assert summary["processing_contract"]["charge_inversion"] is False
    for candidate in [summary["baseline"], *summary["candidates"]]:
        assert candidate["metrics"]["roi"] == spec["roi"]
    assert comparison.submit(service, spec, planned["plan_hash"])["id"] == saved["id"]
    assert sdk.get("retained-isp/comparisons/" + saved["id"])["id"] == saved["id"]
    workbench = sdk.get("retained-isp/workbench")
    assert any(x["source_id"] == source["id"] for x in workbench["sources"])
    assert any(x["id"] == saved["id"] for x in workbench["comparisons"])
    manifest = comparison.export_manifest(service, saved["id"])
    with zipfile.ZipFile(comparison.export_bundle(service, saved["id"])) as archive:
        assert json.loads(archive.read("manifest.json")) == manifest
        for entry in manifest["artifacts"]:
            data = archive.read(f"artifacts/{entry['sha256']}/{entry['name']}")
            assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    np.testing.assert_array_equal(
        load_array(service, source, "raw_groups"),
        load_array(service, service.registry.get_run(source["id"]), "raw_groups"),
    )


def test_ae_comparison_processor_excludes_planning_trace_and_latent_truth(workflow, monkeypatch):
    from camera_platform import retained_isp

    service, _, source, spec = prepared(workflow)
    original = load_array(service, source, "raw_groups")[:, 0]
    planned = comparison.plan(service, spec)
    queued = comparison.submit(service, spec, planned["plan_hash"])
    process = retained_isp.process_raw
    calls = []

    def checked(raw, raw_format, profile, **kwargs):
        assert set(kwargs) == {"geometry", "frame_records", "events"}
        np.testing.assert_array_equal(raw, original)
        assert not any(k in canonical(kwargs) for k in (
            '"frame_ledger"', '"simulation_oracle"', '"mean_electrons"',
            '"planning_contract"', '"control_trace"',
        ))
        assert kwargs["events"] == source["result"]["summary"]["retained_charge"]["events"]
        calls.append(raw.copy())
        return process(raw, raw_format, profile, **kwargs)

    monkeypatch.setattr(retained_isp, "process_raw", checked)
    finish(service, queued)
    assert len(calls) == 3


@pytest.mark.parametrize("field", ["frame_records", "realized_acquisition", "control_trace"])
def test_comparison_verifier_refuses_changed_actual_binding_even_with_rewritten_summary_blob(
    workflow, field,
):
    service, _, _, spec = prepared(workflow)
    planned = comparison.plan(service, spec)
    completed = finish(service, comparison.submit(service, spec, planned["plan_hash"]))
    forged = deepcopy(completed)
    summary = forged["result"]["summary"]
    if field == "frame_records":
        summary[field][1]["applied"]["exposure_ns"] = 4_000_000
    elif field == "realized_acquisition":
        summary[field]["control_sequence"]["frames"][1]["applied"]["exposure_ns"] = 4_000_000
    else:
        summary[field]["commands"][0]["status"] = "invented_applied"
    replacement = service.registry.store_blob(
        canonical(summary).encode(), "summary.json", "application/json",
    )
    forged["result"]["artifacts"] = [
        replacement if a["name"] == "summary.json" else a
        for a in forged["result"]["artifacts"]
    ]
    with pytest.raises(IntegrityError, match="actual acquisition binding"):
        comparison.verify(service, forged)
