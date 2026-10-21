"""Code-domain comparison invariants, durable plan identity and bounded evidence."""

import hashlib
import io
import json
import zipfile
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest
from test_retained_charge_workflow import hdr_workflow as hdr_workflow
from test_retained_charge_workflow import workflow as workflow
from test_retained_isp import inputs, temporal

from camera_platform import retained_isp as code
from camera_platform import retained_isp_comparison as comparison
from camera_platform.registry import ConflictError, IntegrityError, SQLiteRegistry


def specification(**changes):
    result = {
        "schema": comparison.SCHEMA,
        "name": "Authored filter comparison",
        "source_kind": "run",
        "source_id": "original",
        "isp_model_id": "profile",
        "selector": {"schema": "camera_retained_raw_selection_v1", "readout_id": "single"},
        "axes": [{"parameter": "temporal_filter.previous_weight", "values": [0, 0.5]}],
        "roi": {"row_start": 0, "row_end": 8, "column_start": 0, "column_end": 8},
        "objectives": [
            {"metric": "mean_abs_baseline_delta", "direction": "minimize"},
            {"metric": "spatial_tv", "direction": "minimize"},
        ],
        "limits": {"max_frame_mean_abs_delta": 1, "max_frame_temporal_change_distortion": 1},
    }
    result.update(changes)
    return result


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    from camera_platform import retained_isp_replay, retained_isp_source

    registry = SQLiteRegistry(tmp_path / "registry")
    raw = np.full((3, 8, 8), 164, dtype=np.int32)
    raw[0] = 1064
    metadata = inputs(raw)
    stream = io.BytesIO()
    np.save(stream, raw, allow_pickle=False)
    artifact = registry.store_blob(stream.getvalue(), "raw_groups.npy", "application/octet-stream")
    evidence = registry.store_blob(b"bounded factory evidence", "factory.txt", "text/plain")
    source = {
        "source_id": "original",
        "raw_shape": list(raw.shape),
        "raw_container_shape": [3, 1, 8, 8],
        "raw_sha256": artifact["sha256"],
        "selected_array_sha256": hashlib.sha256(raw.tobytes()).hexdigest(),
        "raw_format": metadata["raw_format"],
        "geometry": metadata["geometry"],
        "frame_records": metadata["frame_records"],
        "retained_charge_events": metadata["events"],
        "input_artifacts": [artifact],
        "source_snapshot": {"oracle": "must not be a processing argument"},
    }
    profile = temporal()
    model = {"id": "profile", "kind": "isp", "payload": profile}
    state = {"source": source, "profile": profile, "model": model, "environment": {"id": "first"}}
    export = {
        "run": {"id": "original", "artifacts": [artifact]},
        "models": [model],
        "factory_jobs": [{"id": "factory", "artifacts": [evidence]}],
    }
    service = SimpleNamespace(registry=registry, export_run=lambda _: deepcopy(export))

    def prepare(_service, payload):
        return {
            **deepcopy(payload),
            "job_kind": "isp_replay",
            "plugin_id": "retained_isp_replay_v1",
            "source": deepcopy(state["source"]),
            "profile": deepcopy(state["profile"]),
            "isp_model_snapshot": deepcopy(state["model"]),
            "environment": deepcopy(state["environment"]),
            "input_artifacts": [artifact],
            "evidence_forecast": {"fixed": 1},
        }

    monkeypatch.setattr(retained_isp_replay, "prepare", prepare)
    monkeypatch.setattr(retained_isp_replay, "forecast", lambda *args: {"fixed": 1})
    monkeypatch.setattr(retained_isp_source, "load_inputs", lambda *_: raw.copy())
    return service, raw, state, artifact, evidence


@pytest.mark.parametrize(
    "edit",
    [
        {"axes": [{"parameter": "tone.gamma", "values": [1]}]},
        {"axes": [{"parameter": "detail.amount", "values": [True]}]},
        {"axes": [{"parameter": "temporal_filter.previous_weight", "values": [1]}]},
        {"axes": [{"parameter": "detail.amount", "values": [0, 0]}]},
        {"axes": [{"parameter": "detail.amount", "values": list(np.arange(13) / 13)}]},
        {
            "objectives": [
                {"metric": "mean_abs_baseline_delta", "direction": "maximize"},
                {"metric": "spatial_tv", "direction": "minimize"},
            ]
        },
        {"limits": {"max_frame_mean_abs_delta": 1}},
        {
            "selector": {
                "schema": "camera_retained_raw_selection_v1",
                "readout_id": "single",
                "mode": "all",
            }
        },
        {"force": 1},
        {"roi": {"row_start": -1, "row_end": 8, "column_start": 0, "column_end": 8}},
    ],
)
def test_request_refuses_undeclared_axes_signs_and_coercion(edit):
    with pytest.raises(ValueError):
        comparison.validate_specification(specification(**edit))


def test_rgb_and_temporal_error_cannot_cancel():
    baseline = np.zeros((3, 2, 2, 3), dtype=float)
    candidate = baseline.copy()
    candidate[1] = [1, -1, 0]
    candidate[2] = [-1, 1, 0]
    roi = {"row_start": 0, "row_end": 2, "column_start": 0, "column_end": 2}
    result = comparison.measure(candidate, baseline, roi)
    assert candidate.mean() == 0
    worst = result["worst_frame"]
    assert worst["mean_abs_baseline_delta"]["value"] == pytest.approx(2 / 3)
    assert worst["rms_baseline_delta"]["value"] == pytest.approx(np.sqrt(2 / 3))
    assert worst["max_abs_baseline_delta"]["value"] == 1
    assert worst["temporal_change_distortion"]["value"] == pytest.approx(4 / 3)
    assert result["per_frame"][0]["metrics"]["temporal_change_distortion"]["value"] is None


def test_one_frame_temporal_evidence_stays_unknown_and_ineligible():
    a = np.zeros((1, 8, 8, 3))
    spec = specification()
    metrics = comparison.measure(a, a, spec["roi"])
    ranked = comparison.rank(
        [{"candidate_id": "unknown", "metrics": metrics}], spec["objectives"], spec["limits"]
    )
    assert metrics["worst_frame"]["temporal_change_distortion"]["status"] == "unknown"
    assert ranked["candidates"][0]["feasible"] is False
    assert ranked["pareto_candidate_ids"] == []
    assert ranked["selected_candidate_id"] is None


def test_fidelity_and_variation_tradeoff_has_no_automatic_winner():
    spec = specification()
    baseline = np.tile(np.array([0.0, 1.0] * 4)[None, None, :, None], (2, 8, 1, 3))
    smooth = np.full_like(baseline, 0.5)
    candidates = [
        {"candidate_id": name, "metrics": comparison.measure(a, baseline, spec["roi"])}
        for name, a in (("bypass", baseline), ("smooth", smooth))
    ]
    result = comparison.rank(candidates, spec["objectives"], spec["limits"])
    assert result["pareto_candidate_ids"] == ["bypass", "smooth"]
    assert result["selected_candidate_id"] is None
    limited = comparison.rank(
        candidates, spec["objectives"], {**spec["limits"], "max_frame_mean_abs_delta": 0.1}
    )
    assert limited["pareto_candidate_ids"] == ["bypass"]
    assert limited["candidates"][1]["constraints"][0]["status"] == "fail"


def test_temporal_blend_adds_lag_and_constraint_refuses_it(prepared):
    _, raw, _, _, _ = prepared
    meta = inputs(raw)
    baseline = code.process_raw(raw, profile=code.bypass_profile(temporal()), **meta)["arrays"][
        "pre_tone_code_rgb_frames"
    ]
    candidate = code.process_raw(raw, profile=temporal(), **meta)["arrays"][
        "pre_tone_code_rgb_frames"
    ]
    # Analytic code-domain blend: frame 1 retains half the prior 900 DN contrast.
    assert np.mean(candidate[1] - baseline[1]) == pytest.approx(450 / (4095 - 64))
    spec = specification()
    metrics = comparison.measure(candidate, baseline, spec["roi"])
    limits = {"max_frame_mean_abs_delta": 1, "max_frame_temporal_change_distortion": 0.01}
    result = comparison.rank(
        [{"candidate_id": "lag", "metrics": metrics}], spec["objectives"], limits
    )
    assert result["candidates"][0]["constraints"][1]["status"] == "fail"
    assert result["candidates"][0]["feasible"] is False


def test_plan_detaches_inputs_cache_force_and_historical_environment(prepared):
    service, _, state, _, _ = prepared
    spec = specification()
    planned = comparison.plan(service, spec)
    job = comparison.submit(service, spec, planned["plan_hash"])
    assert job["job_kind"] == comparison.JOB_KIND
    assert comparison.submit(service, spec, planned["plan_hash"])["id"] == job["id"]
    assert (
        comparison.submit(service, {**spec, "force": True}, planned["plan_hash"])["id"] != job["id"]
    )
    spec["axes"][0]["values"][0] = 0.1
    assert job["specification"]["axes"][0]["values"][0] == 0
    with pytest.raises(ConflictError, match="changed after planning"):
        comparison.submit(service, spec, planned["plan_hash"])
    state["environment"] = {"id": "a different environment length"}
    comparison.verify(service, job)
    with pytest.raises(IntegrityError, match="differs"):
        comparison.verify(service, job, current_environment=True)


def test_plan_detects_source_change_and_refuses_inactive_axis(prepared):
    service, _, state, _, _ = prepared
    spec = specification()
    planned = comparison.plan(service, spec)
    state["source"]["source_snapshot"]["oracle"] = "changed evidence"
    with pytest.raises(ConflictError):
        comparison.submit(service, spec, planned["plan_hash"])
    state["profile"]["temporal_filter"]["mode"] = "bypass"
    with pytest.raises(ValueError, match="active"):
        comparison.plan(service, spec)


def test_corrupt_original_artifact_refuses_new_plan_before_queue(prepared):
    service, _, _, original, _ = prepared
    service.registry.blob_path(original["sha256"]).write_bytes(b"not the original RAW")
    with pytest.raises(IntegrityError, match="checksum|bytes"):
        comparison.plan(service, specification())
    assert service.registry.list_runs() == []


@pytest.mark.parametrize("field", ["baseline_profile", "candidates", "cost", "plan_hash"])
def test_frozen_plan_changes_refused_before_processor(prepared, monkeypatch, field):
    service, _, _, _, _ = prepared
    planned = comparison.plan(service, specification())
    job = comparison.submit(service, specification(), planned["plan_hash"])
    altered = deepcopy(job)
    if field == "baseline_profile":
        altered[field]["tone"]["curve"] = "linear"
    elif field == "candidates":
        altered[field][1]["profile"]["temporal_filter"]["previous_weight"] = 0.2
    elif field == "cost":
        altered[field]["archive_bytes_bound"] += 1
    else:
        altered[field] = "0" * 64
    monkeypatch.setattr(
        code, "process_raw", lambda *args, **kwargs: pytest.fail("processor called")
    )
    with pytest.raises(IntegrityError):
        comparison.execute(service, altered)


def test_prequeue_full_candidate_forecast_and_rejection(prepared, monkeypatch):
    service, raw, _, _, _ = prepared
    spec = specification(
        axes=[{"parameter": "temporal_filter.previous_weight", "values": list(np.arange(12) / 20)}]
    )
    cost = comparison.plan(service, spec)["cost"]
    assert cost["candidate_count"] == 12 and cost["processor_invocations"] == 13
    assert cost["output_array_bytes_bound"] == 13 * 2 * (
        raw.size * 3 * 8 + comparison.NPY_HEADER_BYTES_LIMIT
    )
    assert cost["max_preview_count"] == 13 * len(raw)
    assert cost["source_artifact_bytes"] > raw.nbytes
    monkeypatch.setattr(comparison, "MAX_ARCHIVE_BYTES", cost["archive_bytes_bound"] - 1)
    with pytest.raises(ValueError, match="bounded"):
        comparison.plan(service, spec)
    assert service.registry.list_runs() == []


def test_actual_arrays_durable_job_pngs_and_all_original_bundle_members(prepared):
    service, raw, _, original, factory = prepared
    spec = specification()
    planned = comparison.plan(service, spec)
    job = comparison.submit(service, spec, planned["plan_hash"])
    claimed = service.registry.claim()
    result = comparison.execute(service, claimed)
    saved = service.registry.finish(job["id"], result=result)
    verified = comparison.get(service, saved["id"])
    summary = verified["result"]["summary"]
    assert len(summary["candidates"]) == 2
    assert len(summary["preview_groups"]) == 3
    assert summary["ranking"]["selected_candidate_id"] is None
    for case in [summary["baseline"], *summary["candidates"]]:
        artifact = case["arrays"]["rgb_frames"]
        array = np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)
        assert array.shape == (*raw.shape, 3)
    archive_path = comparison.export_bundle(service, saved["id"])
    assert archive_path.stat().st_size <= planned["cost"]["archive_bytes_bound"]
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["comparison"]["plan_hash"] == planned["plan_hash"]
        for artifact in [original, factory, *saved["artifacts"]]:
            data = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
            assert hashlib.sha256(data).hexdigest() == artifact["sha256"]
            assert len(data) == artifact["bytes"]
    artifact = summary["candidates"][1]["arrays"]["rgb_frames"]
    service.registry.blob_path(artifact["sha256"]).write_bytes(b"tampered")
    with pytest.raises(IntegrityError, match="checksum|bytes"):
        comparison.get(service, saved["id"])


def test_cancellation_preserves_original_job_and_has_no_model_promotion(prepared):
    service, _, _, _, _ = prepared
    plan = comparison.plan(service, specification())
    job = comparison.submit(service, specification(), plan["plan_hash"])
    cancelled = comparison.cancel(service, job["id"])
    assert cancelled["status"] == "cancelled"
    assert comparison.list_jobs(service)[0]["id"] == job["id"]
    assert service.registry.claim() is None


@pytest.mark.parametrize(
    "family,readout",
    [("single", "single"), ("nonlinear", "single"), ("paired", "HCG"), ("paired_nonlinear", "LCG")],
)
def test_real_api_worker_comparison_all_electrical_families_and_cold_bundle(
    workflow, family, readout
):
    from test_hdr_workflow import finish
    from test_retained_isp_workflow import acquisition, payload

    from camera_platform.service import PlatformService

    service, sdk, run = acquisition(workflow, family)
    replay = payload(service, sdk, run, readout, enabled=True)
    spec = specification(
        source_id=run["id"],
        isp_model_id=replay["isp_model_id"],
        selector=replay["selector"],
        roi={"row_start": 0, "row_end": 16, "column_start": 0, "column_end": 16},
    )
    models_before = len(service.registry.list("models"))
    planned = sdk.plan_retained_isp_comparison(spec)
    queued = sdk.submit_retained_isp_comparison(planned)
    result = finish(service, queued)
    assert result["status"] == "succeeded"
    summary = result["result"]["summary"]
    assert summary["raw_format"]["response_type"] == (
        "nonlinear_charge_voltage" if "nonlinear" in family else "linear"
    )
    assert summary["selector"]["readout_id"] == readout
    assert len(service.registry.list("models")) == models_before
    assert sdk.submit_retained_isp_comparison(planned)["id"] == result["id"]
    assert sdk.retained_isp_comparison(result["id"])["id"] == result["id"]
    assert not any(j["id"] == result["id"] for j in sdk.get("runs"))
    cold = PlatformService(service.registry.directory)
    assert comparison.get(cold, result["id"])["id"] == result["id"]
    bundle = sdk.retained_isp_comparison_bundle(result["id"])
    assert len(bundle) <= planned["cost"]["archive_bytes_bound"]
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["comparison"]["source_export"]["run"] == run
        assert manifest["comparison"]["source_export"]["factory_jobs"]
        for artifact in manifest["artifacts"]:
            data = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
            assert hashlib.sha256(data).hexdigest() == artifact["sha256"]
    assert service.registry.audit()["integrity"] == "verified"
