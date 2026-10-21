"""One selected original path, actual row times, explicit risk and durable results."""

from copy import deepcopy
from dataclasses import asdict

import numpy as np
import pytest
from test_hdr_workflow import finish, load_array
from test_retained_charge_workflow import hdr_workflow as hdr_workflow
from test_retained_charge_workflow import workflow as workflow
from test_retained_isp_workflow import acquisition

from camera_platform import raw_temporal_replay as replay
from camera_platform.raw_temporal import PairFilterConfig
from camera_platform.registry import IntegrityError, digest
from camera_platform.retained_isp_source import resolve
from camera_platform.service import PlatformService


def payload(run, readout="single"):
    starts = [f["frame_start_ns"] for f in run["result"]["summary"]["control_sequence"]["frames"]]
    return {"schema": replay.SCHEMA, "name": "Original RAW comparison",
            "source_id": run["id"], "readout_id": readout, "cadence_ns": starts[1]-starts[0],
            "config": asdict(PairFilterConfig()), "acknowledge_period_two_risk": True}


@pytest.mark.parametrize("family,path,index", [("single", "single", 0), ("paired", "LCG", 1)])
def test_opt_in_replay_api_persistence_selected_raw_actual_times_and_previews(workflow, family, path, index):
    service, sdk, source_run = acquisition(workflow, family)
    original = deepcopy(source_run)
    spec = payload(source_run, path)
    choices = sdk.raw_temporal_sources()
    assert any(c["source_id"] == source_run["id"] and c["readout_id"] == path for c in choices)
    before = len(service.registry.list_runs())
    response = sdk.http.post("/api/v1/raw-temporal/replays", json={**spec, "acknowledge_period_two_risk": False})
    assert response.status_code == 422
    assert len(service.registry.list_runs()) == before
    queued = sdk.submit_raw_temporal(source_run["id"], path, name=spec["name"],
        cadence_ns=spec["cadence_ns"], config=spec["config"], acknowledge_period_two_risk=True)
    job = finish(service, queued)
    summary = job["result"]["summary"]
    raw = load_array(service, source_run, "raw_groups")[:, index]
    np.testing.assert_array_equal(load_array(service, job, "raw_frames"), raw)
    assert load_array(service, job, "processed_code_dn").dtype == np.float64
    assert load_array(service, job, "blend_mask").dtype == bool
    assert summary["acquisition"]["raw_format"] == source_run["result"]["summary"]["raw_formats"][index]
    assert summary["acquisition"]["frame_conditions"] == job["source"]["frame_conditions"]
    assert summary["acquisition"]["readout_id"] == path
    expected_times = [f["row_transfer_ns"] for f in job["source"]["retained_charge_events"]["frames"]]
    np.testing.assert_array_equal(load_array(service, job, "row_transfer_ns"), expected_times)
    assert summary["future_frames_used"] == 0 and summary["source_truth_arrays_used"] is False
    assert not summary["default_enabled"] and "Genuine period-two" in summary["limitation"]
    assert set(summary["frames"][0]["previews"]) == {"raw_frames", "processed_code_dn", "blend_mask"}
    for url in summary["frames"][0]["previews"].values():
        png = sdk.http.get(url)
        assert png.status_code == 200 and png.content.startswith(b"\x89PNG")
    assert sdk.raw_temporal_replay(job["id"])["status"] == "succeeded"
    assert sdk.get("raw-temporal/replays")[0]["id"] == job["id"]
    assert replay.submit(service, spec)["id"] == job["id"]
    assert sdk.http.get(f"/api/v1/raw-temporal/replays/{job['id']}/export").status_code == 200
    reopened = PlatformService(service.registry.directory, workspace=service.workspace)
    assert replay.get(reopened, job["id"])["result"] == job["result"]
    assert service.registry.get_run(source_run["id"]) == original
    assert all(r["id"] != job["id"] for r in service.list_simulation_runs())
    assert sdk.http.get("/raw-temporal.html").status_code == 200
    assert sdk.http.get("/raw-temporal.js").status_code == 200


def test_list_is_metadata_only_and_detail_rejects_damaged_raw(workflow, monkeypatch):
    service, sdk, run = acquisition(workflow)
    job = finish(service, replay.submit(service, payload(run)))
    with monkeypatch.context() as patch:
        patch.setattr(replay, "prepare", lambda *a: pytest.fail("List reverified source bytes"))
        rows = replay.list_records(service)
        assert len(rows) == 1 and rows[0]["id"] == job["id"] and "source" not in rows[0]
    artifact = next(a for a in run["artifacts"] if a["name"] == "raw_groups.npy")
    service.registry.blob_path(artifact["sha256"]).write_bytes(b"damaged test-only RAW")
    with pytest.raises(IntegrityError):
        replay.get(service, job["id"])


def test_adapter_resets_every_pd_clear_and_settings_change(workflow):
    service, sdk, run = acquisition(workflow)
    source = resolve(service, run["id"], {"schema": "camera_retained_raw_selection_v1", "readout_id": "single"})
    raw = load_array(service, run, "raw_groups")[:, 0]
    spec = payload(run)
    # These are unit-level, self-consistent acquisition contracts, not new registry facts.
    cleared = deepcopy(source)
    events = cleared["retained_charge_events"]
    for frame in events["frames"]:
        frame["clear_before_exposure"] = True
    events["schedule_sha256"] = digest({k: v for k, v in events.items() if k != "schedule_sha256"})
    output = replay.process_verified(cleared, raw, spec)
    assert [f["reset_reason"] for f in output["summary"]["frames"]] == ["pd_clear"]*len(raw)
    assert not output["arrays"]["blend_mask"].any()
    changed = deepcopy(source)
    changed["frame_records"][2]["applied"]["gain"] = 2.0
    result = replay.process_verified(changed, raw, spec)
    assert result["summary"]["frames"][2]["reset_reason"] == "settings_change"
    wrong_cadence = replay.process_verified(source, raw, {**spec, "cadence_ns": spec["cadence_ns"]+1})
    assert not wrong_cadence["arrays"]["blend_mask"].any()
    assert wrong_cadence["summary"]["frames"][1]["reset_reason"] == "row_cadence_change"


def test_realized_electrical_ae_is_replayed_not_confused_with_initial_plan(workflow):
    from test_trapped_control_workflow import create_program
    service, sdk, *_ = workflow
    _, _, request = create_program(workflow)
    run = finish(service, service.submit_run(request))
    actual = run["result"]["summary"]["control_sequence"]
    assert actual != run["control_sequence_snapshot"]["payload"]
    job = finish(service, replay.submit(service, payload(run)))
    conditions = job["result"]["summary"]["acquisition"]["frame_conditions"]
    assert [f["exposure_ns"] for f in conditions] == [f["applied"]["exposure_ns"] for f in actual["frames"]]
    assert any(f["reset_reason"] in {"row_cadence_change", "settings_change"}
               for f in job["result"]["summary"]["frames"][1:])


@pytest.mark.parametrize("change", [{"acknowledge_period_two_risk": 1}, {"cadence_ns": True},
    {"config": {}}, {"readout_id": "automatic"}, {"schema": "camera_retained_code_isp_replay_v1"}])
def test_explicit_contract_rejects_ambiguous_or_legacy_request(change):
    spec = {"schema": replay.SCHEMA, "name": "test", "source_id": "run-test", "readout_id": "single",
            "cadence_ns": 20_000_000, "config": asdict(PairFilterConfig()), "acknowledge_period_two_risk": True}
    with pytest.raises(ValueError):
        replay.specification({**spec, **change})
