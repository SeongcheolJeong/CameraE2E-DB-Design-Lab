"""Registered, sequential Worker jobs for observable-only ISP optimization."""

from __future__ import annotations

from copy import deepcopy

from . import retained_isp_observable_optimizer as optimizer
from .imaging import _current
from .registry import ConflictError, IntegrityError, canonical, digest

SELECTION_JOB = "retained_isp_observable_selection"
HOLDOUT_JOB = "retained_isp_observable_holdout"
PLUGIN_ID = "retained_isp_observable_optimizer_v1"
SELECTION_PLAN = "camera_retained_isp_observable_selection_plan_v1"
HOLDOUT_PLAN = "camera_retained_isp_observable_holdout_plan_v1"


def _selection_frozen(service, payload, *, historical_environment=None):
    request = optimizer.validate_request(payload)
    selection = optimizer._selection(service, request)
    return {
        "job_kind": SELECTION_JOB, "plugin_id": PLUGIN_ID,
        "specification": request, "selection": selection,
        "environment": deepcopy(_current(service) if historical_environment is None
                                else historical_environment),
        "cost": {"schema": SELECTION_PLAN,
                 "candidate_count": len(selection["development_rows"]),
                 "max_report_bytes": optimizer.MAX_REPORT_BYTES,
                 "holdout_output_arrays_read_at_plan": False},
    }


def plan_selection(service, payload):
    frozen = _selection_frozen(service, payload)
    return {"plan_hash": digest(frozen), "frozen_request": frozen,
            "cost": deepcopy(frozen["cost"])}


def submit_selection(service, payload, plan_hash):
    proposed = plan_selection(service, payload)
    if not isinstance(plan_hash, str) or plan_hash != proposed["plan_hash"]:
        raise ConflictError("Observable ISP development selection changed after planning")
    document = {**proposed["frozen_request"], "plan_hash": plan_hash}
    job = service.registry.submit(document, digest(document))
    return verify_selection_job(service, job)


def verify_selection_job(service, job, *, current_environment=False):
    if job.get("job_kind") != SELECTION_JOB or job.get("plugin_id") != PLUGIN_ID:
        raise ValueError("Job is not an observable ISP selection")
    fresh = _selection_frozen(
        service, job["specification"],
        historical_environment=None if current_environment else job["environment"],
    )
    if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
        raise IntegrityError("Observable ISP selection differs from frozen development inputs")
    if job.get("plan_hash") != digest(fresh):
        raise IntegrityError("Observable ISP selection plan hash differs")
    result = job.get("result")
    if result is not None:
        if (result.get("physical_validation") != "not_established"
                or canonical(result.get("summary")) != canonical(fresh["selection"])
                or not isinstance(result.get("artifacts"), list)
                or len(result["artifacts"]) != 1):
            raise IntegrityError("Observable ISP selection result differs from frozen choice")
        if canonical(optimizer.verify_selection(service, result["artifacts"][0])) != canonical(
                fresh["selection"]):
            raise IntegrityError("Observable ISP selection artifact differs")
    return job


def execute_selection(service, job):
    verify_selection_job(service, job, current_environment=True)
    frozen = optimizer.freeze_selection(service, job["specification"])
    if canonical(frozen["selection"]) != canonical(job["selection"]):
        raise IntegrityError("Observable ISP development changed during Worker execution")
    return {"summary": frozen["selection"], "artifacts": [frozen["artifact"]],
            "physical_validation": "not_established"}


def _holdout_frozen(service, selection_job_id, *, historical_environment=None):
    if (not isinstance(selection_job_id, str) or not selection_job_id
            or selection_job_id != selection_job_id.strip()
            or len(selection_job_id) > 200):
        raise ValueError("Holdout requires a bounded selection job ID")
    selected_job = verify_selection_job(service, service.registry.get_run(selection_job_id))
    if selected_job["status"] != "succeeded" or selected_job.get("result") is None:
        raise ValueError("Holdout requires a completed frozen selection job")
    selection_artifact = selected_job["result"]["artifacts"][0]
    selection = selected_job["result"]["summary"]
    development = service.registry.get_run(selection["request"]["development_joint_study_id"])
    holdout = service.registry.get_run(selection["request"]["holdout_joint_study_id"])
    provenance = optimizer._compatible(development, holdout)
    if any(selection["provenance"].get(key) != value
           for key, value in provenance.items()):
        raise IntegrityError("Holdout acquisition changed after selection was frozen")
    scored = (0 if selection["selected"] is None else
              1 if selection["selected"]["candidate_id"]
              == selection["request"]["baseline_candidate_id"] else 2)
    return {
        "job_kind": HOLDOUT_JOB, "plugin_id": PLUGIN_ID,
        "selection_job_id": selection_job_id,
        "selection_job_content_hash": selected_job["content_hash"],
        "selection_artifact": deepcopy(selection_artifact),
        "holdout_job_id": holdout["id"],
        "holdout_job_content_hash": holdout["content_hash"],
        "environment": deepcopy(_current(service) if historical_environment is None
                                else historical_environment),
        "cost": {"schema": HOLDOUT_PLAN, "max_report_bytes": optimizer.MAX_REPORT_BYTES,
                 "candidate_profiles_cold_verified": (
                     holdout["cost"]["candidate_count"] if scored else 0),
                 "candidate_profiles_scored": scored,
                 "no_reselection_after_holdout": True},
    }


def plan_holdout(service, selection_job_id):
    frozen = _holdout_frozen(service, selection_job_id)
    return {"plan_hash": digest(frozen), "frozen_request": frozen,
            "cost": deepcopy(frozen["cost"])}


def submit_holdout(service, selection_job_id, plan_hash):
    proposed = plan_holdout(service, selection_job_id)
    if not isinstance(plan_hash, str) or plan_hash != proposed["plan_hash"]:
        raise ConflictError("Observable ISP holdout lineage changed after planning")
    document = {**proposed["frozen_request"], "plan_hash": plan_hash}
    job = service.registry.submit(document, digest(document))
    return verify_holdout_job(service, job)


def verify_holdout_job(service, job, *, current_environment=False):
    if job.get("job_kind") != HOLDOUT_JOB or job.get("plugin_id") != PLUGIN_ID:
        raise ValueError("Job is not an observable ISP holdout")
    fresh = _holdout_frozen(
        service, job["selection_job_id"],
        historical_environment=None if current_environment else job["environment"],
    )
    if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
        raise IntegrityError("Observable ISP holdout differs from frozen selection/source")
    if job.get("plan_hash") != digest(fresh):
        raise IntegrityError("Observable ISP holdout plan hash differs")
    result = job.get("result")
    if result is not None:
        if (result.get("physical_validation") != "not_established"
                or not isinstance(result.get("artifacts"), list)
                or len(result["artifacts"]) != 1):
            raise IntegrityError("Observable ISP holdout result is malformed")
        actual = optimizer.verify_assessment(
            service, fresh["selection_artifact"], result["artifacts"][0],
        )
        if canonical(result.get("summary")) != canonical(actual):
            raise IntegrityError("Observable ISP holdout result differs from saved arrays")
    return job


def execute_holdout(service, job):
    verify_holdout_job(service, job, current_environment=True)
    assessed = optimizer.assess_holdout(service, job["selection_artifact"])
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
        raise ValueError("Observable ISP selection is not complete")
    return {"schema": "camera_retained_isp_observable_selection_export_v1",
            "job": job, "selection_artifact": job["result"]["artifacts"][0],
            "development_joint_study_id": job["specification"]["development_joint_study_id"],
            "holdout_joint_study_id": job["specification"]["holdout_joint_study_id"],
            "physical_validation": "not_established"}


def export_holdout(service, identity):
    job = get_holdout_job(service, identity)
    if job["status"] != "succeeded":
        raise ValueError("Observable ISP holdout is not complete")
    return {"schema": "camera_retained_isp_observable_holdout_export_v1",
            "job": job, "selection_job_id": job["selection_job_id"],
            "assessment_artifact": job["result"]["artifacts"][0],
            "physical_validation": "not_established"}
