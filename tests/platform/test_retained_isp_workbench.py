"""One verified history read, original detail obligations, detached compact display."""

import pytest
from test_hdr_workflow import finish
from test_retained_charge_workflow import hdr_workflow as hdr_workflow
from test_retained_charge_workflow import workflow as workflow
from test_retained_isp_comparison import specification
from test_retained_isp_workflow import acquisition, payload

from camera_platform import retained_isp_comparison as comparison
from camera_platform import retained_isp_replay as replay
from camera_platform import retained_isp_workbench as workbench
from camera_platform.registry import IntegrityError, canonical, digest
from camera_platform.service import PlatformService


def originals(registry):
    with registry.connection() as conn:
        return {name: [tuple(row) for row in conn.execute("SELECT * FROM " + name)]
                for name in ("records", "jobs", "audit")}


def completed(workflow, family="single", readout="single"):
    service, sdk, source = acquisition(workflow, family)
    request = payload(service, sdk, source, readout, enabled=True)
    saved_replay = finish(service, replay.submit(service, request))
    spec = specification(source_id=source["id"], isp_model_id=request["isp_model_id"],
                         selector=request["selector"])
    plan = comparison.plan(service, spec)
    saved_comparison = finish(service, comparison.submit(service, spec, plan["plan_hash"]))
    return service, sdk, source, saved_replay, saved_comparison


@pytest.mark.parametrize("family,readout", [
    ("single", "single"), ("nonlinear", "single"),
    ("paired", "HCG"), ("paired_nonlinear", "LCG"),
])
def test_api_composite_matches_all_constituents_once_without_processing(
    workflow, monkeypatch, family, readout
):
    service, sdk, source, _, _ = completed(workflow, family, readout)
    sources = replay.choices(service)
    models = [m for m in service.registry.list("models") if m["kind"] == "isp"
              and m["payload"].get("schema") == replay.PROFILE_SCHEMA]
    replays = [r for r in service.list_replays() if r["plugin_id"] == replay.PLUGIN_ID]
    comparisons = comparison.list_jobs(service)
    before = originals(service.registry)
    calls = {"jobs": 0, "models": 0}
    runs, records = service.registry.list_runs, service.registry.list

    def listed_runs():
        calls["jobs"] += 1
        return runs()

    def listed_records(kind):
        if kind == "models":
            calls["models"] += 1
        return records(kind)

    monkeypatch.setattr(service.registry, "list_runs", listed_runs)
    monkeypatch.setattr(service.registry, "list", listed_records)
    monkeypatch.setattr(service.registry, "get_run",
                        lambda *_: pytest.fail("fresh job hydration escaped snapshot"))
    monkeypatch.setattr(service.registry, "store_blob", lambda *_: pytest.fail("GET wrote blob"))
    monkeypatch.setattr(service, "execute_run", lambda *_: pytest.fail("GET processed pixels"))
    result = sdk.get("retained-isp/workbench")
    assert result["schema"] == workbench.SCHEMA
    assert result["sources"] == sources
    assert result["source_discovery"] == {
        "scope": "metadata_only", "raw_integrity": "verified_on_selection",
        "physical_validation": "not_established",
    }
    assert calls == {"jobs": 1, "models": 1}
    for compact, original in zip(result["models"], models, strict=True):
        assert {k: compact[k] for k in workbench.MODEL_FIELDS} == {
            k: original.get(k) for k in workbench.MODEL_FIELDS}
        assert compact["full_record_sha256"] == digest(original)
        assert compact["detail_path"] == "/models/" + original["id"]
    for collection, old, route in (("replays", replays, "/replays"),
                                   ("comparisons", comparisons, "/retained-isp/comparisons")):
        for compact, original in zip(result[collection], old, strict=True):
            for key in workbench.HISTORY_FIELDS:
                assert compact[key] == original.get(key, original.get("specification", {}).get(key))
            assert compact["full_record_sha256"] == digest(original)
            assert compact["detail_path"] == route + "/" + original["id"]
            assert not {"result", "source", "base_replay", "input_artifacts"} & compact.keys()
    assert result["sources"][0]["raw_format"] in source["result"]["summary"]["raw_formats"]
    assert len(canonical(result).encode()) < 25_000
    assert originals(service.registry) == before


@pytest.mark.parametrize("target", ["unrelated_request", "unrelated_result", "completion", "model"])
def test_all_history_integrity_is_checked_before_filter_and_on_later_call(tmp_path, target):
    service = PlatformService(tmp_path / "registry")
    model = service.registry.put("models", {"kind": "lens", "payload": {"untouched": 1}})
    job = service.registry.submit({"job_kind": "unrelated", "request": {"value": 1}}, "other")
    service.registry.claim()
    service.registry.finish(job["id"], result={"value": 1})
    assert workbench.build(service)["replays"] == []
    with service.registry.connection() as conn:
        if target == "model":
            conn.execute("DROP TRIGGER immutable_records_update")
            conn.execute("UPDATE records SET document='{}' WHERE id=?", (model["id"],))
        elif target == "completion":
            conn.execute("DROP TRIGGER immutable_audit_update")
            conn.execute("UPDATE audit SET event_hash=? WHERE action='runs.succeeded'",
                         ("f" * 64,))
        else:
            conn.execute("DROP TRIGGER immutable_completed_job")
            field = "document" if target == "unrelated_request" else "result"
            conn.execute("UPDATE jobs SET " + field + "='{}' WHERE id=?", (job["id"],))
    with pytest.raises(IntegrityError):
        workbench.build(service)


@pytest.mark.parametrize("target", [
    "source_raw", "source_input", "replay_array", "replay_summary", "replay_preview",
    "comparison_array", "comparison_summary", "comparison_preview", "missing_preview",
])
def test_later_equal_size_or_missing_evidence_is_not_cached(workflow, target):
    service, _, source, saved_replay, saved_comparison = completed(workflow)
    assert workbench.build(service)["replays"][0]["id"] == saved_replay["id"]
    if target == "source_raw":
        entry = next(a for a in source["artifacts"] if a["name"] == "raw_groups.npy")
    elif target == "source_input":
        entry = source["input_artifacts"][0]
    else:
        record = saved_comparison if target.startswith("comparison") else saved_replay
        if target.endswith("summary"):
            entry = next(a for a in record["artifacts"] if a["name"] == "summary.json")
        elif target.endswith("array"):
            entry = next(a for a in record["artifacts"]
                         if a["name"].endswith(".npy") and "rgb" in a["name"])
        else:
            entry = next(a for a in record["artifacts"] if a["media_type"] == "image/png")
    path = service.registry.blob_path(entry["sha256"])
    if target == "missing_preview":
        path.unlink()
    else:
        original = path.read_bytes()
        path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        assert path.stat().st_size == entry["bytes"]
    expected_error = KeyError if target == "missing_preview" else IntegrityError
    with pytest.raises(expected_error):
        workbench.build(service)


def test_snapshot_keeps_queued_transition_coherent_and_next_call_is_fresh(workflow, monkeypatch):
    service, sdk, source = acquisition(workflow)
    queued = replay.submit(service, payload(service, sdk, source))
    listed = service.registry.list_runs
    finished = []

    def finish_after_snapshot():
        jobs = listed()
        finished.append(finish(service, queued))
        return jobs

    with monkeypatch.context() as context:
        context.setattr(service.registry, "list_runs", finish_after_snapshot)
        response = workbench.build(service)
    row = response["replays"][0]
    assert row["status"] == "queued" and row["full_record_sha256"] == digest(queued)
    second = workbench.build(service)["replays"][0]
    assert second["status"] == "succeeded"
    assert second["full_record_sha256"] == digest(finished[0])


def test_detached_getters_and_returned_payloads_do_not_change_later_reads(workflow):
    service, _, _, saved_replay, _ = completed(workflow)
    snapshot = workbench._SnapshotRegistry(service.registry)
    model_id = saved_replay["isp_model_id"]
    snapshot.get_run(saved_replay["id"])["source"]["selector"]["readout_id"] = "edited"
    snapshot.get("models", model_id)["payload"]["temporal_filter"]["previous_weight"] = 0.9
    assert snapshot.get_run(saved_replay["id"]) == saved_replay
    assert snapshot.get("models", model_id) == service.registry.get("models", model_id)
    before = workbench.build(service)
    changed = workbench.build(service)
    changed["sources"][0]["raw_shape"][0] = 999
    changed["models"][0]["payload"]["temporal_filter"]["previous_weight"] = 0.9
    changed["replays"][0]["selector"]["readout_id"] = "edited"
    assert workbench.build(service) == before
    later_job = service.registry.submit({"job_kind": "unrelated"}, "later")
    later_model = service.registry.put("models", {"kind": "lens", "payload": {}})
    with pytest.raises(KeyError, match="snapshot"):
        snapshot.get_run(later_job["id"])
    with pytest.raises(KeyError, match="snapshot"):
        snapshot.get("models", later_model["id"])


def test_response_excludes_unrelated_payloads_and_refuses_over_budget(tmp_path):
    service = PlatformService(tmp_path / "registry")
    large = "x" * workbench.MAX_RESPONSE_BYTES
    service.registry.put("models", {"kind": "lens", "payload": {"large": large}})
    service.registry.submit({"job_kind": "unrelated", "request": {"large": large}}, "large")
    small = workbench.build(service)
    assert len(canonical(small).encode()) < 1024
    assert small["verification"]["jobs_verified"] == 1
    assert small["verification"]["models_verified"] == 1
    service.registry.put("models", {"kind": "isp", "payload": {
        "schema": replay.PROFILE_SCHEMA, "large": large}})
    with pytest.raises(ValueError, match="exceeds 4 MiB; no history was truncated"):
        workbench.build(service)


def test_api_response_budget_returns_explicit_refusal(workflow, monkeypatch):
    service, sdk, *_ = workflow
    monkeypatch.setattr(workbench, "MAX_RESPONSE_BYTES", 1)
    response = sdk.http.get("/api/v1/retained-isp/workbench")
    assert response.status_code == 422
    assert "no history was truncated" in response.json()["error"]
