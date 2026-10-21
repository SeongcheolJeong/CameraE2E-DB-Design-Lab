"""Registered choice-before-observation ISP jobs with cold evidence verification."""

from __future__ import annotations

from copy import deepcopy

from . import retained_isp_joint_workflow as joint
from . import retained_isp_prospective_optimizer as engine
from .imaging import _current
from .registry import ConflictError, IntegrityError, canonical, digest

SELECTION_JOB = "retained_isp_prospective_selection"
HOLDOUT_JOB = "retained_isp_prospective_holdout"
PLUGIN_ID = "retained_isp_prospective_optimizer_v1"
SELECTION_PLAN = "camera_retained_isp_prospective_selection_plan_v1"
HOLDOUT_PLAN = "camera_retained_isp_prospective_holdout_plan_v1"


def _selection_frozen(service, payload, *, historical_environment=None):
    request = engine.validate_request(payload)
    selection = engine._selection(service, request)
    return {
        "job_kind": SELECTION_JOB,
        "plugin_id": PLUGIN_ID,
        "specification": request,
        "selection": selection,
        "environment": deepcopy(
            _current(service) if historical_environment is None else historical_environment
        ),
        "cost": {
            "schema": SELECTION_PLAN,
            "candidate_count": len(selection["development_rows"]),
            "max_report_bytes": engine.observable.MAX_REPORT_BYTES,
            "holdout_id_known_at_selection": False,
            "holdout_output_arrays_read_at_selection": False,
        },
    }


def plan_selection(service, payload):
    frozen = _selection_frozen(service, payload)
    return {"plan_hash": digest(frozen), "frozen_request": frozen,
            "cost": deepcopy(frozen["cost"])}


def submit_selection(service, payload, plan_hash):
    proposed = plan_selection(service, payload)
    if not isinstance(plan_hash, str) or plan_hash != proposed["plan_hash"]:
        raise ConflictError("Prospective ISP development changed after planning")
    document = {**proposed["frozen_request"], "plan_hash": plan_hash}
    return verify_selection_job(service, service.registry.submit(document, digest(document)))


def verify_selection_job(service, job, *, current_environment=False):
    if job.get("job_kind") != SELECTION_JOB or job.get("plugin_id") != PLUGIN_ID:
        raise ValueError("Job is not a prospective ISP selection")
    fresh = _selection_frozen(
        service, job["specification"],
        historical_environment=None if current_environment else job["environment"],
    )
    if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
        raise IntegrityError("Prospective ISP selection differs from development inputs")
    if job.get("plan_hash") != digest(fresh):
        raise IntegrityError("Prospective ISP selection plan hash differs")
    result = job.get("result")
    if result is not None:
        if (result.get("physical_validation") != "not_established"
                or canonical(result.get("summary")) != canonical(fresh["selection"])
                or not isinstance(result.get("artifacts"), list)
                or len(result["artifacts"]) != 1):
            raise IntegrityError("Prospective ISP selection result differs from locked choice")
        if canonical(engine.verify_selection(service, result["artifacts"][0])) != canonical(
                fresh["selection"]):
            raise IntegrityError("Prospective ISP selection artifact differs")
    return job


def execute_selection(service, job):
    verify_selection_job(service, job, current_environment=True)
    frozen = engine.freeze_selection(service, job["specification"])
    if canonical(frozen["selection"]) != canonical(job["selection"]):
        raise IntegrityError("Prospective ISP development changed during Worker execution")
    return {"summary": frozen["selection"], "artifacts": [frozen["artifact"]],
            "physical_validation": "not_established"}


def _holdout_frozen(service, selection_job_id, holdout_joint_study_id,
                    *, historical_environment=None):
    engine.observable._identity(selection_job_id, "selection_job_id")
    engine.observable._identity(holdout_joint_study_id, "holdout_joint_study_id")
    selected_job = verify_selection_job(service, service.registry.get_run(selection_job_id))
    if selected_job["status"] != "succeeded" or selected_job.get("result") is None:
        raise ValueError("Prospective holdout requires a completed frozen selection")
    if selected_job["result"]["summary"]["selected"] is None:
        raise ValueError("No feasible development candidate can enter prospective holdout")
    holdout = service.registry.get_run(holdout_joint_study_id)
    if (holdout.get("status") != "succeeded" or holdout.get("job_kind") != joint.JOB_KIND
            or holdout.get("plugin_id") != joint.PLUGIN_ID
            or holdout.get("cost", {}).get("schema")
            != "camera_retained_isp_joint_budget_v3"):
        raise ValueError("Prospective holdout requires a completed Joint Study v3")
    holdout = joint.verify(service, holdout)
    provenance = engine.holdout_provenance(service, selected_job, holdout)
    chosen = selected_job["result"]["summary"]["selected"]["candidate_id"]
    baseline = selected_job["specification"]["baseline_candidate_id"]
    scored = 1 if chosen == baseline else 2
    return {
        "job_kind": HOLDOUT_JOB,
        "plugin_id": PLUGIN_ID,
        "selection_job_id": selection_job_id,
        "selection_job_content_hash": selected_job["content_hash"],
        "selection_artifact": deepcopy(selected_job["result"]["artifacts"][0]),
        "holdout_joint_study_id": holdout_joint_study_id,
        "holdout_joint_study_content_hash": holdout["content_hash"],
        "provenance": provenance,
        "environment": deepcopy(
            _current(service) if historical_environment is None else historical_environment
        ),
        "cost": {
            "schema": HOLDOUT_PLAN,
            "candidate_profiles_cold_verified": holdout["cost"]["candidate_count"],
            "candidate_profiles_scored": scored,
            "max_report_bytes": engine.observable.MAX_REPORT_BYTES,
            "original_source_count_fingerprint_checked": len(engine.observable._bindings(holdout)),
            "no_reselection_after_holdout": True,
        },
    }


def plan_holdout(service, selection_job_id, holdout_joint_study_id):
    frozen = _holdout_frozen(service, selection_job_id, holdout_joint_study_id)
    return {"plan_hash": digest(frozen), "frozen_request": frozen,
            "cost": deepcopy(frozen["cost"])}


def submit_holdout(service, selection_job_id, holdout_joint_study_id, plan_hash):
    proposed = plan_holdout(service, selection_job_id, holdout_joint_study_id)
    if not isinstance(plan_hash, str) or plan_hash != proposed["plan_hash"]:
        raise ConflictError("Prospective holdout source changed after planning")
    document = {**proposed["frozen_request"], "plan_hash": plan_hash}
    return verify_holdout_job(service, service.registry.submit(document, digest(document)))


def verify_holdout_job(service, job, *, current_environment=False):
    if job.get("job_kind") != HOLDOUT_JOB or job.get("plugin_id") != PLUGIN_ID:
        raise ValueError("Job is not a prospective ISP holdout")
    fresh = _holdout_frozen(
        service, job["selection_job_id"], job["holdout_joint_study_id"],
        historical_environment=None if current_environment else job["environment"],
    )
    if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
        raise IntegrityError("Prospective holdout differs from frozen observations")
    if job.get("plan_hash") != digest(fresh):
        raise IntegrityError("Prospective holdout plan hash differs")
    result = job.get("result")
    if result is not None:
        if (result.get("physical_validation") != "not_established"
                or not isinstance(result.get("artifacts"), list)
                or len(result["artifacts"]) != 1):
            raise IntegrityError("Prospective holdout result authority differs")
        selection_job = verify_selection_job(
            service, service.registry.get_run(job["selection_job_id"]),
        )
        holdout = service.registry.get_run(job["holdout_joint_study_id"])
        actual = engine.verify_assessment(
            service, selection_job, holdout, result["artifacts"][0],
        )
        if canonical(result.get("summary")) != canonical(actual):
            raise IntegrityError("Prospective holdout result differs from saved evidence")
    return job


def execute_holdout(service, job):
    verify_holdout_job(service, job, current_environment=True)
    selection_job = verify_selection_job(
        service, service.registry.get_run(job["selection_job_id"]),
    )
    holdout = service.registry.get_run(job["holdout_joint_study_id"])
    assessed = engine.assess_holdout(service, selection_job, holdout)
    if canonical(assessed["assessment"]["provenance"]) != canonical(job["provenance"]):
        raise IntegrityError("Prospective holdout provenance changed during Worker execution")
    return {"summary": assessed["assessment"], "artifacts": [assessed["artifact"]],
            "physical_validation": "not_established"}


def list_selection_jobs(service):
    return [verify_selection_job(service, job) for job in service.registry.list_runs()
            if job.get("job_kind") == SELECTION_JOB]


def list_holdout_jobs(service):
    return [verify_holdout_job(service, job) for job in service.registry.list_runs()
            if job.get("job_kind") == HOLDOUT_JOB]


def get_selection_job(service, identity):
    return verify_selection_job(service, service.registry.get_run(identity))


def get_holdout_job(service, identity):
    return verify_holdout_job(service, service.registry.get_run(identity))


def export_selection(service, identity):
    job = get_selection_job(service, identity)
    if job["status"] != "succeeded":
        raise ValueError("Prospective ISP selection is incomplete")
    return {
        "schema": "camera_retained_isp_prospective_selection_export_v1",
        "job": job,
        "selection_artifact": job["result"]["artifacts"][0],
        "physical_validation": "not_established",
    }


def export_holdout(service, identity):
    job = get_holdout_job(service, identity)
    if job["status"] != "succeeded":
        raise ValueError("Prospective ISP holdout is incomplete")
    return {
        "schema": "camera_retained_isp_prospective_holdout_export_v1",
        "job": job,
        "selection_job_id": job["selection_job_id"],
        "assessment_artifact": job["result"]["artifacts"][0],
        "physical_validation": "not_established",
    }
