"""A prospective choice names no holdout until after its completed lock."""

from copy import deepcopy

import numpy as np
import pytest
from test_retained_isp_observable_optimizer import _array
from test_retained_isp_observable_optimizer import _request as retrospective_request
from test_retained_isp_observable_optimizer import saved_jobs as saved_jobs  # noqa: F401

from camera_platform import retained_isp_prospective_optimizer as prospective
from camera_platform.registry import IntegrityError, canonical


def _request():
    request = retrospective_request()
    request["schema"] = prospective.SCHEMA
    del request["holdout_joint_study_id"]
    return request


def _locked(saved_jobs, monkeypatch, request=None):
    request = _request() if request is None else request
    frozen = prospective.freeze_selection(saved_jobs, request)
    summary = frozen["selection"]
    lock_at = "2026-09-27T02:00:00+00:00"
    selected_job = {
        "id": "locked-selection", "content_hash": "locked-selection-hash",
        "status": "succeeded", "updated_at": lock_at,
        "result": {"summary": summary, "artifacts": [frozen["artifact"]]},
    }
    development = saved_jobs.registry.jobs["development"]
    holdout = saved_jobs.registry.jobs["holdout"]
    development["temporal_reference_snapshot"] = {
        "id": "development-reference",
    }
    holdout["temporal_reference_snapshot"] = {
        "id": "new-reference", "created_at": "2026-09-27T02:00:03+00:00",
    }
    holdout["created_at"] = "2026-09-27T02:00:04+00:00"
    for index in range(5):
        identity = f"holdout-source-{index}"
        saved_jobs.registry.jobs[identity] = {
            "id": identity, "created_at": "2026-09-27T02:00:01+00:00",
        }
    monkeypatch.setattr(prospective, "_frame_fingerprints", lambda _service, job: {
        ("development" if job["id"] == "development" else "holdout") + "-frame"
    })
    return selected_job, holdout


def test_selection_never_looks_up_or_names_a_holdout(saved_jobs, monkeypatch):
    original = saved_jobs.registry.get_run

    def guarded(identity):
        if identity == "holdout":
            raise AssertionError("Selection must not look up any holdout job")
        return original(identity)

    monkeypatch.setattr(saved_jobs.registry, "get_run", guarded)
    frozen = prospective.freeze_selection(saved_jobs, _request())
    selected = frozen["selection"]
    assert selected["selected"]["candidate_id"] == "candidate-001"
    assert selected["holdout_job_id"] is None
    assert selected["holdout_outputs_read"] is False
    assert selected["holdout_blinding_enforced"] is False
    assert "holdout_joint_study_id" not in selected["request"]
    assert prospective.verify_selection(saved_jobs, frozen["artifact"]) == selected


def test_future_observations_and_frame_pixel_disjointness_are_required(
    saved_jobs, monkeypatch,
):
    selected_job, holdout = _locked(saved_jobs, monkeypatch)
    proof = prospective.holdout_provenance(saved_jobs, selected_job, holdout)
    assert proof["selection_completed_at"] < proof["holdout_joint_created_at"]
    assert proof["blinding_enforced"] is False
    assert len(proof["holdout_original_run_creation"]) == 5

    old = deepcopy(holdout)
    old["created_at"] = selected_job["updated_at"]
    with pytest.raises(ValueError, match="Holdout Joint Study"):
        prospective.holdout_provenance(saved_jobs, selected_job, old)

    old = deepcopy(holdout)
    old["temporal_reference_snapshot"]["created_at"] = selected_job["updated_at"]
    with pytest.raises(ValueError, match="temporal reference"):
        prospective.holdout_provenance(saved_jobs, selected_job, old)

    old_run = saved_jobs.registry.jobs["holdout-source-0"]
    old_run["created_at"] = selected_job["updated_at"]
    with pytest.raises(ValueError, match="original RAW Run"):
        prospective.holdout_provenance(saved_jobs, selected_job, holdout)
    old_run["created_at"] = "2026-09-27T02:00:01+00:00"

    monkeypatch.setattr(prospective, "_frame_fingerprints", lambda _service, _job: {
        "same-original-frame"
    })
    with pytest.raises(ValueError, match="original development RAW frame"):
        prospective.holdout_provenance(saved_jobs, selected_job, holdout)


def test_prospective_assessment_is_relative_and_never_physical_pass(saved_jobs, monkeypatch):
    selected_job, holdout = _locked(saved_jobs, monkeypatch)
    assessed = prospective.assess_holdout(saved_jobs, selected_job, holdout)
    report = assessed["assessment"]
    assert report["consistency_status"] in {"consistent", "inconsistent", "inconclusive"}
    assert report["physical_validation"] == "not_established"
    assert report["holdout_blinding_enforced"] is False
    assert report["no_reselection_after_holdout"] is True
    assert set(report["holdout_metrics"]) == {"candidate-000", "candidate-001"}
    assert prospective.verify_assessment(
        saved_jobs, selected_job, holdout, assessed["artifact"],
    ) == report

    forged = deepcopy(report)
    forged["physical_validation"] = "pass"
    fake = saved_jobs.registry.store_blob(
        canonical(forged).encode(), prospective.ASSESSMENT_ARTIFACT,
        "application/json",
    )
    with pytest.raises(IntegrityError, match="assessment differs"):
        prospective.verify_assessment(saved_jobs, selected_job, holdout, fake)


def test_request_has_no_holdout_id_or_pass_claim():
    request = _request()
    request["holdout_joint_study_id"] = "premature-holdout"
    with pytest.raises(ValueError, match="exactly"):
        prospective.validate_request(request)


def _display_guard_request():
    request = _request()
    for item in request["constraints"]:
        if item["metric"].startswith("display_"):
            item["value"] = 1
    request["objectives"].append({
        "metric": prospective.DISPLAY_TIME, "weight": 0.1, "scale": 0.1,
    })
    request["constraints"].append({
        "metric": prospective.DISPLAY_TIME, "operator": "<=", "value": 0.1,
        "unit": "normalized_display_rgb",
    })
    return request


def test_display_motion_guard_rejects_flat_output_even_with_preserved_pre_tone(
    saved_jobs,
):
    job = saved_jobs.registry.jobs["development"]
    candidate = job["result"]["summary"]["cases"][1]
    pre_tone = np.broadcast_to(
        np.asarray([0.2, 0.2, 0.8])[:, None, None, None], (3, 4, 4, 3),
    ).copy()
    flat_display = np.full((3, 4, 4, 3), 0.2)
    candidate["dynamic_processing"]["pre_tone_artifact"] = _array(
        saved_jobs.registry, "preserved-pre-tone.npy", pre_tone,
    )
    candidate["dynamic_processing"]["display_rgb_artifact"] = _array(
        saved_jobs.registry, "flattened-display.npy", flat_display,
    )
    request = _display_guard_request()
    previous = deepcopy(request)
    previous["objectives"].pop()
    previous["constraints"].pop()
    assert prospective.freeze_selection(saved_jobs, previous)["selection"][
        "selected"]["candidate_id"] == "candidate-001"

    frozen = prospective.freeze_selection(saved_jobs, request)
    selection = frozen["selection"]
    flattened = next(row for row in selection["development_rows"]
                     if row["candidate_id"] == "candidate-001")
    assert flattened["metrics"]["temporal_change_rms_delta"] == 0
    assert flattened["metrics"][prospective.DISPLAY_TIME] > 0.1
    assert flattened["feasibility"] == "infeasible"
    assert selection["selected"]["candidate_id"] == "candidate-000"
    assert prospective.verify_selection(saved_jobs, frozen["artifact"]) == selection


def test_display_motion_guard_is_unknown_if_baseline_has_no_display_motion(saved_jobs):
    baseline = saved_jobs.registry.jobs["development"]["result"]["summary"]["cases"][0]
    baseline["dynamic_processing"]["display_rgb_artifact"] = _array(
        saved_jobs.registry, "static-baseline-display.npy",
        np.full((3, 4, 4, 3), 0.2),
    )
    selection = prospective.freeze_selection(saved_jobs, _display_guard_request())["selection"]
    assert selection["selected"] is None
    assert all(row["metrics"][prospective.DISPLAY_TIME] is None
               and row["feasibility"] != "feasible"
               and "Baseline display has no resolvable" in row["unknown_reason"]
               for row in selection["development_rows"])


def test_locked_display_motion_guard_detects_holdout_collapse(saved_jobs, monkeypatch):
    request = _display_guard_request()
    holdout_candidate = saved_jobs.registry.jobs["holdout"]["result"]["summary"]["cases"][1]
    holdout_candidate["dynamic_processing"]["display_rgb_artifact"] = _array(
        saved_jobs.registry, "holdout-flattened-display.npy",
        np.full((3, 4, 4, 3), 0.2),
    )
    selected_job, holdout = _locked(saved_jobs, monkeypatch, request=request)
    assert selected_job["result"]["summary"]["selected"]["candidate_id"] == "candidate-001"
    assessed = prospective.assess_holdout(saved_jobs, selected_job, holdout)
    report = assessed["assessment"]
    assert report["consistency_status"] == "inconsistent"
    assert next(item for item in report["holdout_constraints"]
                if item["metric"] == prospective.DISPLAY_TIME)["status"] == "exceeded"
    assert prospective.verify_assessment(
        saved_jobs, selected_job, holdout, assessed["artifact"],
    ) == report


def test_declared_dynamic_roi_detects_motion_loss_outside_noise_roi(
    saved_jobs, monkeypatch,
):
    job = saved_jobs.registry.jobs["development"]
    job["engine_specification"]["noise_roi"] = {
        "row_start": 0, "row_end": 2, "column_start": 0, "column_end": 2,
    }
    candidate = job["result"]["summary"]["cases"][1]
    pre_tone = np.broadcast_to(
        np.asarray([0.2, 0.2, 0.8])[:, None, None, None], (3, 4, 4, 3),
    ).copy()
    display = pre_tone.copy()
    display[:, 2:, 2:, :] = 0.2
    candidate["dynamic_processing"]["pre_tone_artifact"] = _array(
        saved_jobs.registry, "roi-preserved-pre-tone.npy", pre_tone,
    )
    candidate["dynamic_processing"]["display_rgb_artifact"] = _array(
        saved_jobs.registry, "roi-flattened-display.npy", display,
    )
    request = _display_guard_request()
    legacy = prospective.freeze_selection(saved_jobs, request)["selection"]
    assert legacy["selected"]["candidate_id"] == "candidate-001"

    request["dynamic_roi"] = {
        "row_start": 2, "row_end": 4, "column_start": 2, "column_end": 4,
    }
    monkeypatch.setattr(prospective.retained_isp_source, "load_inputs",
                        lambda _service, _source: np.broadcast_to(
                            np.asarray([1000, 1000, 2000], dtype=np.int32)[:, None, None],
                            (3, 4, 4),
                        ).copy())
    frozen = prospective.freeze_selection(saved_jobs, request)
    chosen = frozen["selection"]
    flattened = next(row for row in chosen["development_rows"]
                     if row["candidate_id"] == "candidate-001")
    assert flattened["metrics"]["stationary_repeat_residual_std"] == 0.04
    assert flattened["metrics"]["temporal_change_rms_delta"] == 0
    assert flattened["metrics"][prospective.DISPLAY_TIME] > 0.1
    assert flattened["dynamic_support"]["evaluation_roi"] == request["dynamic_roi"]
    assert flattened["feasibility"] == "infeasible"
    assert chosen["selected"]["candidate_id"] == "candidate-000"
    assert prospective.verify_selection(saved_jobs, frozen["artifact"]) == chosen


def test_dynamic_roi_must_fit_native_support(saved_jobs):
    request = _display_guard_request()
    request["dynamic_roi"] = {
        "row_start": 2, "row_end": 5, "column_start": 2, "column_end": 4,
    }
    with pytest.raises(ValueError, match="dynamic ROI exceeds native support"):
        prospective.freeze_selection(saved_jobs, request)
