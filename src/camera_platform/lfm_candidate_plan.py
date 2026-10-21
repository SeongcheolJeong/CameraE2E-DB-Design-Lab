"""Read-only candidate preflight across frozen models and whole-period PWM policy.

The planner never synthesizes RAW, changes model applicability, or submits jobs.
An eligible point still needs a new immutable Control/Test and full Worker preflight.
"""

from __future__ import annotations

import math
from copy import deepcopy

from . import electrical_control
from .composer import check_assembly
from .condition_space import REQUEST_SCHEMA as SPACE_REQUEST_SCHEMA
from .condition_space import analyze_records
from .electrical_control_runtime import (
    PLUGIN_ID, TRAPPED_PROGRAM_SCHEMA, observation_path, validate_capture_program,
)
from .gain_semantics import model_declarations
from .registry import IntegrityError
from .timing import MAX_EXPOSURE_NS

REQUEST_SCHEMA = "camera_lfm_candidate_plan_request_v1"
RESULT_SCHEMA = "camera_lfm_candidate_plan_result_v1"
MAX_CANDIDATES = 16
MIN_ANALOG_GAIN = 1e-6
MAX_ANALOG_GAIN = 1e6


def validate_request(value):
    if not isinstance(value, dict) or set(value) != {"schema", "source_run_id", "candidates"}:
        raise ValueError("LFM candidate plan needs schema, source_run_id and candidates")
    if value["schema"] != REQUEST_SCHEMA:
        raise ValueError("Unsupported LFM candidate plan request")
    identity, candidates = value["source_run_id"], value["candidates"]
    if not isinstance(identity, str) or not identity.startswith("run-") or len(identity) > 100:
        raise ValueError("LFM candidate plan needs a saved run ID")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise ValueError("LFM candidate plan needs 1..16 candidate points")
    points = set()
    for item in candidates:
        if (not isinstance(item, dict) or set(item) != {"exposure_ns", "gain"}
                or type(item["exposure_ns"]) is not int
                or not 1 <= item["exposure_ns"] <= MAX_EXPOSURE_NS
                or type(item["gain"]) not in (int, float)
                or not math.isfinite(item["gain"]) or item["gain"] <= 0):
            raise ValueError("Candidate needs a positive integer-ns exposure and finite positive gain")
        point = item["exposure_ns"], item["gain"]
        if point in points:
            raise ValueError("LFM candidate points must be distinct")
        points.add(point)
    return deepcopy(value)


def _source(job):
    if job.get("status") != "succeeded" or job.get("plugin_id") != PLUGIN_ID:
        raise ValueError("LFM candidate plan requires a completed electrical control run")
    assembly = job["assembly_snapshot"]
    models = job["model_snapshots"]
    frozen = job["capture_program_snapshot"]
    control = job["control_sequence_snapshot"]
    if (assembly["id"] != job["assembly_id"]
            or [model["id"] for model in models] != assembly["model_ids"]
            or frozen["id"] != job["capture_program_id"]
            or frozen["payload"] != job["request"]["capture_program"]
            or control["id"] != job["control_sequence_id"]
            or control["payload"] != job["request"]["control_sequence"]):
        raise IntegrityError("Saved LFM run source snapshots differ from its frozen request")
    summary = job["result"]["summary"]
    if (summary["capture_program"] != frozen["payload"]
            or summary["physical_validation"] != "not_established"):
        raise IntegrityError("Saved LFM result differs from its frozen source")
    # The frozen control is an initial scaffold, not the realized AE sequence.
    # Do not compare them for equality: legitimate feedback changes exposure,
    # commands and latch metadata. The service entry verifies the realized
    # sequence against original RAW through the existing acquisition verifier.
    program = validate_capture_program(frozen["payload"])
    policy = electrical_control.validate_policy(program["electrical_control_policy"])
    index, readout_id, _ = observation_path(program)
    if (program["schema"] != TRAPPED_PROGRAM_SCHEMA
            or program["electrical_model"]["schema"] != "camera_electrical_readout_v1"
            or policy["schema"] != electrical_control.PERIOD_POLICY_SCHEMA
            or index != 0 or readout_id != "single"):
        raise ValueError("Plan supports single linear trapped AE under a whole-period PWM policy")
    frames = control["payload"]["frames"]
    exposures = {frame["applied"]["exposure_ns"] for frame in frames}
    gains = {frame["applied"]["gain"] for frame in frames}
    if len(exposures) != 1 or len(gains) != 1:
        raise ValueError("Plan requires fixed initial exposure and gain across the scaffold")
    return assembly, models, frozen, policy, next(iter(exposures)), next(iter(gains))


def analyze_job(job, specification):
    """Project candidate metadata from a service-verified acquisition record.

    This pure helper does not certify original RAW or realized control evidence.
    External callers must use ``analyze`` for that verification boundary.
    """
    spec = validate_request(specification)
    if job["id"] != spec["source_run_id"]:
        raise ValueError("Candidate plan source run ID differs from the frozen run")
    assembly, models, program, policy, initial_exposure, initial_gain = _source(job)
    exposures = list(dict.fromkeys(item["exposure_ns"] for item in spec["candidates"]))
    gains = list(dict.fromkeys(item["gain"] for item in spec["candidates"]))
    space = analyze_records(assembly, models, {
        "schema": SPACE_REQUEST_SCHEMA, "assembly_id": assembly["id"],
        "candidates": {"exposure_s": [value / 1e9 for value in exposures], "gain": gains},
    })
    exposure_space = dict(zip(exposures, space["axes"]["exposure_s"]["candidates"], strict=True))
    gain_space = dict(zip(gains, space["axes"]["gain"]["candidates"], strict=True))
    gain_roles = model_declarations(models)
    gain_role_complete = all(item["status"] == "declared" for item in gain_roles)
    grid = electrical_control.effective_exposure_grid(policy)
    outcomes = []
    for item in spec["candidates"]:
        exposure, gain = item["exposure_ns"], item["gain"]
        exposure_model, gain_model = exposure_space[exposure], gain_space[gain]
        on_grid = (grid["minimum"] <= exposure <= grid["maximum"]
                   and (exposure - grid["minimum"]) % grid["step"] == 0)
        if not policy["enabled"]:
            on_grid = exposure == initial_exposure
        readout_gain = MIN_ANALOG_GAIN <= gain <= MAX_ANALOG_GAIN
        conditions = deepcopy(assembly["conditions"])
        conditions.update(exposure_s={"value": exposure / 1e9, "unit": "s"},
                          gain={"value": gain, "unit": "1"})
        joint = check_assembly({**assembly, "conditions": conditions}, models)
        reasons = []
        if exposure_model["status"] != "compatible":
            reasons.append("model_exposure_domain_" + exposure_model["status"])
        if gain_model["status"] != "compatible":
            reasons.append("model_gain_domain_" + gain_model["status"])
        if not on_grid:
            reasons.append("outside_effective_pwm_exposure_lattice")
        if not readout_gain:
            reasons.append("outside_linear_readout_gain_implementation")
        if gain != initial_gain and not gain_role_complete:
            reasons.append("analog_gain_model_roles_undeclared")
        if joint["status"] != "PASS":
            reasons.append("joint_composer_" + joint["status"].lower())
        blocked = (exposure_model["status"] == "blocked"
                   or gain_model["status"] == "blocked"
                   or not on_grid or not readout_gain
                   or joint["status"] in {"FAIL", "OUT_OF_DOMAIN"})
        outcomes.append({
            "exposure_ns": exposure, "gain": float(gain),
            "status": ("blocked" if blocked else "unknown" if reasons
                       else "eligible_for_new_frozen_run_preflight"),
            "model_exposure_status": exposure_model["status"],
            "model_gain_status": gain_model["status"],
            "policy_exposure_permitted": on_grid,
            "readout_gain_implementation_permitted": readout_gain,
            "joint_composer_status": joint["status"],
            "blocking_model_ids": sorted(set(exposure_model["blocking_models"])
                                         | set(gain_model["blocking_models"])),
            "unresolved_gain_role_model_ids": ([item["model_id"] for item in gain_roles
                                                if item["status"] != "declared"]
                                               if gain != initial_gain else []),
            "reasons": reasons,
        })
    return {
        "schema": RESULT_SCHEMA,
        "source_run_id": job["id"],
        "source_run_content_hash": job["content_hash"],
        "assembly_id": assembly["id"],
        "assembly_content_hash": assembly["content_hash"],
        "capture_program_id": program["id"],
        "capture_program_content_hash": program["content_hash"],
        "initial_exposure_ns": initial_exposure,
        "initial_gain": initial_gain,
        "effective_exposure_grid_ns": grid,
        "model_gain_intersection": space["axes"]["gain"]["intersection"],
        "candidate_gain_application": "electrical_readout_analog_gain",
        "frozen_electrical_digital_gain": program["payload"]["electrical_model"]["digital_gain"],
        "model_gain_role_declarations": gain_roles,
        "model_exposure_intersection": space["axes"]["exposure_s"]["intersection"],
        "target_black_relative_dn": deepcopy(policy["target_black_relative_dn"]),
        "candidates": outcomes,
        "automatic_submission": False,
        "execution_feasibility": "requires_new_control_test_and_full_worker_preflight",
        "target_feasibility": "not_established",
        "physical_lfm_performance": "not_established",
    }


def analyze(service, specification):
    spec = validate_request(specification)
    job = service.registry.get_run(spec["source_run_id"])
    # Validate the supported topology first, then reuse RAW-only causal replay
    # and exact observed row-event checks. No camera physics is recomputed and
    # no trap-state truth is used. This is one selected record, not a list scan.
    _source(job)
    from .electrical_isp_source import _resolve_verified_record

    _resolve_verified_record(
        service, job["id"],
        {"schema": "camera_retained_raw_selection_v1", "readout_id": "single"}, job,
    )
    return analyze_job(job, spec)
