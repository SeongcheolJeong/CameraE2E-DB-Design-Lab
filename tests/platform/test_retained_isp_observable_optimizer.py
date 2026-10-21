"""Cold selection/holdout boundaries for saved retained-code ISP outputs."""

from __future__ import annotations

import hashlib
import io
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest

from camera_platform import retained_isp_joint_workflow as joint
from camera_platform import retained_isp_observable_optimizer as optimizer
from camera_platform.registry import IntegrityError, canonical
from camera_platform.service import PlatformService


class _Jobs:
    def __init__(self, real):
        self.real = real
        self.jobs = {}

    def get_run(self, identity):
        return deepcopy(self.jobs[identity])

    def store_blob(self, *args):
        return self.real.store_blob(*args)

    def blob_path(self, *args):
        return self.real.blob_path(*args)


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _array(registry, name, value):
    stream = io.BytesIO()
    np.save(stream, value, allow_pickle=False)
    return registry.store_blob(stream.getvalue(), name, "application/x-npy")


def _bindings(prefix):
    result = []
    for index in range(5):
        label = f"{prefix}-source-{index}"
        result.append({
            "source_id": label,
            "assembly_id": "same-assembly",
            "selector": {"schema": "camera_retained_raw_selection_v1",
                         "readout_id": "single"},
            "selected_array_sha256": _sha(label + "-raw"),
            "source_snapshot_sha256": _sha(label + "-snapshot"),
            "raw_format": {"bit_depth": 12, "black_level_dn": 64},
            "geometry": {"rows": 4, "columns": 4},
            "pixel_pitch_um": 1.4,
            "model_parts_sha256": _sha("same camera/readout"),
            "raw_shape": [3, 4, 4],
        })
    return result


def _job(registry, prefix, *, noise=(0.10, 0.04, 0.01), levels=(0.8, 0.7, 0.3)):
    bindings = _bindings(prefix)
    cases = []
    for index, (repeat_noise, last_level) in enumerate(zip(noise, levels, strict=True)):
        ident = f"candidate-{index:03d}"
        code = np.broadcast_to(np.asarray([0.2, 0.2, last_level], dtype=np.float64)
                               [:, None, None, None], (3, 4, 4, 3)).copy()
        display = code.copy()
        if index:
            display[..., 0] += 0.01 if index == 1 else 0.1
        record = {
            "pre_tone_artifact": _array(registry, f"{prefix}-{ident}-pre-tone.npy", code),
            "display_rgb_artifact": _array(registry, f"{prefix}-{ident}-display-rgb.npy",
                                           display),
        }
        cases.append({
            "candidate_id": ident, "profile_sha256": _sha(ident),
            "metrics": {optimizer.engine.NOISE: {
                "value": repeat_noise, "status": "ok", "reason": None,
                "unit": "normalized_code_rgb",
            }},
            "dynamic_processing": record,
        })
    summary = {"cases": cases}
    summary_artifact = registry.store_blob(
        canonical(summary).encode(), "summary.json", "application/json",
    )
    return {
        "id": prefix, "status": "succeeded", "job_kind": joint.JOB_KIND,
        "plugin_id": joint.PLUGIN_ID,
        "content_hash": _sha(prefix + "-job"),
        "plan_hash": _sha(prefix + "-plan"),
        "cost": {"schema": "camera_retained_isp_joint_budget_v3",
                 "candidate_count": 3},
        "isp_model_snapshot": {"id": "same-isp", "payload": {"tone": "linear"}},
        "engine_specification": {
            "isp_model_sha256": _sha("same ISP"),
            "base_profile_sha256": _sha("same base profile"),
            "axes": [{"parameter": "temporal_filter.previous_weight",
                      "values": [0, 0.5, 0.9]}],
            "noise_roi": {"row_start": 0, "row_end": 4,
                          "column_start": 0, "column_end": 4},
            "stationary_bindings": bindings[:4], "dynamic_binding": bindings[-1],
        },
        "dynamic_source_snapshot": {
            "raw_shape": [3, 4, 4], "source_id": f"{prefix}-source-4",
            "raw_format": {"bit_depth": 12, "black_level_dn": 64},
        },
        "result": {"summary": summary, "artifacts": [summary_artifact]},
    }


def _request():
    return {
        "schema": optimizer.SCHEMA, "name": "Noise/time/color bounded search",
        "development_joint_study_id": "development",
        "holdout_joint_study_id": "holdout",
        "baseline_candidate_id": "candidate-000",
        "objectives": [
            {"metric": optimizer.engine.NOISE, "weight": 2, "scale": 0.1},
            {"metric": "temporal_change_rms_delta", "weight": 1, "scale": 0.1},
            {"metric": "display_chroma_rms_delta", "weight": 0.2, "scale": 0.05},
        ],
        "constraints": [
            {"metric": "temporal_change_rms_delta", "operator": "<=", "value": 0.15,
             "unit": "normalized_code_rgb"},
            {"metric": "display_chroma_rms_delta", "operator": "<=", "value": 0.05,
             "unit": "normalized_display_rgb"},
            {"metric": "display_rgb_mae_delta", "operator": "<=", "value": 0.05,
             "unit": "normalized_display_rgb"},
        ],
    }


@pytest.fixture
def saved_jobs(tmp_path, monkeypatch):
    real = PlatformService(tmp_path / "observable-artifacts").registry
    registry = _Jobs(real)
    registry.jobs["development"] = _job(registry, "development")
    registry.jobs["holdout"] = _job(
        registry, "holdout", noise=(0.10, 0.02, 0.001), levels=(0.9, 0.8, 0.2),
    )
    service = SimpleNamespace(registry=registry)
    # Joint Study's own registered workflow tests exercise its full cold verifier;
    # this focused fixture isolates the optimizer's read and selection boundary.
    monkeypatch.setattr(joint, "verify", lambda _service, job: job)
    monkeypatch.setattr(optimizer, "load_inputs", lambda _service, _source: np.broadcast_to(
        np.asarray([1000, 1000, 2000], dtype=np.int32)[:, None, None],
        (3, 4, 4),
    ).copy())
    return service


def test_freeze_before_holdout_and_cold_assessment_only_selected(saved_jobs, monkeypatch):
    loaded = []
    original = joint._load_saved_array

    def tracking_loader(service, artifact, shape, dtype):
        loaded.append(artifact["name"])
        return original(service, artifact, shape, dtype)

    monkeypatch.setattr(joint, "_load_saved_array", tracking_loader)
    frozen = optimizer.freeze_selection(saved_jobs, _request())
    assert frozen["selection"]["selected"]["candidate_id"] == "candidate-001"
    assert frozen["selection"]["holdout_outputs_read"] is False
    assert not any(name.startswith("holdout-") for name in loaded)
    assert optimizer.verify_selection(saved_jobs, frozen["artifact"]) == frozen["selection"]
    loaded.clear()
    assessed = optimizer.assess_holdout(saved_jobs, frozen["artifact"])
    assert assessed["assessment"]["selected"]["candidate_id"] == "candidate-001"
    assert assessed["assessment"]["holdout_status"] == "pass"
    assert assessed["assessment"]["no_reselection_after_holdout"] is True
    assert assessed["assessment"]["selected_minus_baseline_score"] < 0
    assert set(assessed["assessment"]["holdout_metrics"]) == {
        "candidate-000", "candidate-001",
    }
    assert not any(name.startswith("holdout-candidate-002") for name in loaded)
    assert optimizer.verify_assessment(
        saved_jobs, frozen["artifact"], assessed["artifact"],
    ) == assessed["assessment"]


def test_holdout_regression_fails_without_reselection(saved_jobs):
    holdout = saved_jobs.registry.jobs["holdout"]
    holdout["result"]["summary"]["cases"][1]["metrics"][optimizer.engine.NOISE][
        "value"] = 0.5
    frozen = optimizer.freeze_selection(saved_jobs, _request())
    assert frozen["selection"]["selected"]["candidate_id"] == "candidate-001"
    assessed = optimizer.assess_holdout(saved_jobs, frozen["artifact"])["assessment"]
    assert assessed["holdout_status"] == "fail"
    assert assessed["selected"]["candidate_id"] == "candidate-001"
    assert assessed["selected_minus_baseline_score"] > 0


def test_reused_source_or_raw_is_never_accepted_as_holdout(saved_jobs):
    alias = saved_jobs.registry.jobs["holdout"]["engine_specification"][
        "stationary_bindings"][0]
    alias["source_id"] = saved_jobs.registry.jobs["development"][
        "engine_specification"]["stationary_bindings"][0]["source_id"]
    with pytest.raises(ValueError, match="reuses development"):
        optimizer.freeze_selection(saved_jobs, _request())
    alias["source_id"] = "independent-id"
    alias["selected_array_sha256"] = saved_jobs.registry.jobs["development"][
        "engine_specification"]["dynamic_binding"]["selected_array_sha256"]
    with pytest.raises(ValueError, match="original RAW"):
        optimizer.freeze_selection(saved_jobs, _request())


def test_holdout_camera_assembly_and_readout_must_match(saved_jobs):
    dynamic = saved_jobs.registry.jobs["holdout"]["engine_specification"][
        "dynamic_binding"]
    dynamic["assembly_id"] = "different-assembly"
    with pytest.raises(ValueError, match="assembly_id"):
        optimizer.freeze_selection(saved_jobs, _request())
    dynamic["assembly_id"] = "same-assembly"
    dynamic["selector"]["readout_id"] = "HCG"
    with pytest.raises(ValueError, match="selector"):
        optimizer.freeze_selection(saved_jobs, _request())


def test_no_feasible_candidate_never_reads_holdout_output(saved_jobs, monkeypatch):
    request = _request()
    request["constraints"].append({
        "metric": optimizer.engine.NOISE, "operator": "<=", "value": 0,
        "unit": "normalized_code_rgb",
    })
    frozen = optimizer.freeze_selection(saved_jobs, request)
    assert frozen["selection"]["selected"] is None

    original = joint._load_saved_array

    def forbidden(service, artifact, shape, dtype):
        if artifact["name"].startswith("holdout-"):
            raise AssertionError("No feasible development candidate must not read holdout")
        return original(service, artifact, shape, dtype)

    monkeypatch.setattr(joint, "_load_saved_array", forbidden)
    assessed = optimizer.assess_holdout(saved_jobs, frozen["artifact"])
    assert assessed["assessment"]["holdout_status"].startswith("not_run")


@pytest.mark.parametrize("raw_codes,reason", [
    ([1000, 1000, 1000], "no observed frame-to-frame change"),
    ([64, 1000, 2000], "black/white code rail"),
])
def test_time_tradeoff_requires_observed_unsaturated_dynamic_raw(
    saved_jobs, monkeypatch, raw_codes, reason,
):
    monkeypatch.setattr(optimizer, "load_inputs", lambda _service, _source: np.broadcast_to(
        np.asarray(raw_codes, dtype=np.int32)[:, None, None], (3, 4, 4),
    ).copy())
    frozen = optimizer.freeze_selection(saved_jobs, _request())["selection"]
    assert frozen["selected"] is None
    assert all(row["feasibility"] != "feasible" for row in frozen["development_rows"])
    assert all(reason in row["unknown_reason"] for row in frozen["development_rows"])


def test_candidate_clipped_pre_tone_cannot_be_selected(saved_jobs):
    job = saved_jobs.registry.jobs["development"]
    candidate = job["result"]["summary"]["cases"][1]
    code = np.broadcast_to(
        np.asarray([0.2, 0.2, 0.7], dtype=np.float64)[:, None, None, None],
        (3, 4, 4, 3),
    ).copy()
    code[-1, 1, 1, 0] = 1.2
    candidate["dynamic_processing"]["pre_tone_artifact"] = _array(
        saved_jobs.registry, "candidate-001-clipped-pre-tone.npy", code,
    )
    frozen = optimizer.freeze_selection(saved_jobs, _request())["selection"]
    clipped = next(row for row in frozen["development_rows"]
                   if row["candidate_id"] == "candidate-001")
    assert clipped["metrics"]["temporal_change_rms_delta"] is None
    assert clipped["feasibility"] != "feasible"
    assert "Candidate dynamic pre-tone ROI contains clipped code" == clipped[
        "unknown_reason"]
    assert frozen["selected"]["candidate_id"] == "candidate-000"


def test_selection_and_assessment_forgery_rejected(saved_jobs):
    frozen = optimizer.freeze_selection(saved_jobs, _request())
    assessed = optimizer.assess_holdout(saved_jobs, frozen["artifact"])
    fake = deepcopy(frozen["selection"])
    fake["selected"]["candidate_id"] = "candidate-002"
    fake_artifact = saved_jobs.registry.store_blob(
        canonical(fake).encode(), "isp-observable-selection.json", "application/json",
    )
    with pytest.raises(IntegrityError, match="selection differs"):
        optimizer.verify_selection(saved_jobs, fake_artifact)
    fake_assessment = deepcopy(assessed["assessment"])
    fake_assessment["holdout_status"] = "fail"
    fake_artifact = saved_jobs.registry.store_blob(
        canonical(fake_assessment).encode(), "isp-observable-holdout.json",
        "application/json",
    )
    with pytest.raises(IntegrityError, match="holdout differs"):
        optimizer.verify_assessment(saved_jobs, frozen["artifact"], fake_artifact)


def test_weight_scale_color_unit_and_alias_are_frozen(saved_jobs):
    request = _request()
    request["objectives"][0]["scale"] = 0
    with pytest.raises(ValueError, match="positive"):
        optimizer.freeze_selection(saved_jobs, request)
    request = _request()
    request["constraints"][1]["unit"] = "normalized_code_rgb"
    with pytest.raises(ValueError, match="metric/unit"):
        optimizer.freeze_selection(saved_jobs, request)
    request = _request()
    request["holdout_joint_study_id"] = request["development_joint_study_id"]
    with pytest.raises(ValueError, match="different Joint Study"):
        optimizer.freeze_selection(saved_jobs, request)
