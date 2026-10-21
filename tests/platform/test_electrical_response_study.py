"""Real matched acquisitions, immutable planning and durable partial evidence."""

import hashlib
import json
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from test_electrical_control_workflow import workflow as workflow
from test_hdr_workflow import load_array
from test_hdr_workflow import workflow as _hdr_workflow
from test_retained_charge_workflow import workflow as _retained_workflow

from camera_platform import electrical_response_study as study
from camera_platform.contracts import CaptureProgramCreate, ControlSequenceCreate
from camera_platform.registry import ConflictError, IntegrityError, canonical

hdr_workflow = _hdr_workflow
retained_workflow = _retained_workflow


@pytest.fixture
def experiment(workflow):
    service, _, assembly, scene, control, program, *_ = workflow
    spec = study.template(service, assembly["id"], scene["id"], control["id"], program["id"])
    return service, spec


def snapshot(service):
    registry = service.registry
    with registry.connection() as conn:
        rows = {
            table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)]
            for table in ("records", "jobs", "audit")
        }
    return rows, sorted((p.name, p.read_bytes()) for p in registry.blobs.iterdir() if p.is_file())


def finish_all(service):
    jobs = []
    while job := service.registry.claim():
        try:
            result = service.execute_run(job)
            result = service.registry.finish(job["id"], result=result)
        except Exception as error:
            service.registry.finish(
                job["id"], result=getattr(error, "evidence_result", None), error=str(error)
            )
            raise
        jobs.append(result)
    return jobs


def test_plan_is_read_only_deterministic_and_fixes_only_enabled_difference(experiment):
    service, spec = experiment
    before = snapshot(service)
    first, second = study.plan(service, spec), study.plan(service, spec)
    assert canonical(first) == canonical(second)
    assert snapshot(service) == before
    requests = first["frozen_request"]["planned_requests"]
    assert len(requests) == 2
    fixed, active = [deepcopy(r["request"]["request"]["capture_program"]) for r in requests]
    assert fixed["electrical_control_policy"].pop("enabled") is False
    assert active["electrical_control_policy"].pop("enabled") is True
    assert fixed == active
    assert requests[0]["request"]["parameters"] == requests[1]["request"]["parameters"]
    assert first["cost"]["capture_run_count"] == 2


def test_real_pair_raw_changes_after_latch_and_all_bytes_survive_export(experiment):
    service, spec = experiment
    proposal = study.plan(service, spec)
    parent = study.submit(service, spec, proposal["plan_hash"])
    jobs = finish_all(service)
    assert jobs[-1]["id"] == parent["id"] and len(jobs) == 3
    record = study.get(service, parent["id"])
    assert record["status"] == "succeeded"
    result = record["result"]["summary"]
    assert result["status"] == "complete" and result["complete_pair_count"] == 1
    cell = result["cells"][0]
    baseline, candidate = [
        service.registry.get_run(cell[r]["run_id"]) for r in ("baseline", "candidate")
    ]
    a, b = [load_array(service, job, "raw_groups") for job in (baseline, candidate)]
    np.testing.assert_array_equal(a[0], b[0])
    assert not np.array_equal(a[1:], b[1:])
    assert cell["comparison"]["paired_frame_count"] > 0
    assert cell["candidate"]["control_trace"]["commands"]
    assert not cell["baseline"]["control_trace"]["commands"]
    assert (
        cell["baseline"]["actual_timing"][1]["applied_exposure_ns"]
        != cell["candidate"]["actual_timing"][1]["applied_exposure_ns"]
    )
    assert study.submit(service, spec, proposal["plan_hash"])["id"] == parent["id"]
    assert len(study.list_jobs(service)) == 1
    before = snapshot(service)
    exported = study.export_manifest(service, parent["id"])
    assert snapshot(service) == before
    assert len(exported["children"]) == 2
    with zipfile.ZipFile(study.export_bundle(service, parent["id"])) as archive:
        saved = json.loads(archive.read("manifest.json"))
        assert saved == exported
        for artifact in exported["artifacts"]:
            content = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
            assert len(content) == artifact["bytes"]
            assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
    for role in ("baseline", "candidate"):
        for frame in cell[role]["frames"]:
            assert any(a["sha256"] == frame["preview_artifact_id"] for a in record["artifacts"])


@pytest.mark.parametrize("field", ["policy", "raw", "preview", "summary"])
def test_fresh_reads_and_cache_refuse_original_or_result_same_size_corruption(experiment, field):
    service, spec = experiment
    planned = study.plan(service, spec)
    parent = study.submit(service, spec, planned["plan_hash"])
    finish_all(service)
    record = study.get(service, parent["id"])
    child = service.registry.get_run(record["children"][0]["run_id"])
    if field == "policy":
        artifact = service.registry.get("capture_programs", spec["capture_program_id"])[
            "artifacts"
        ][0]
    else:
        name = {"raw": "raw_groups.npy", "summary": "summary.json"}.get(field)
        artifact = (
            next(a for a in child["artifacts"] if a["name"] == name)
            if name
            else next(a for a in child["artifacts"] if a["media_type"] == "image/png")
        )
    path = service.registry.blob_path(artifact["sha256"])
    content = path.read_bytes()
    path.write_bytes(bytes([content[0] ^ 1]) + content[1:])
    with pytest.raises((IntegrityError, ValueError)):
        study.get(service, parent["id"])
    with pytest.raises((IntegrityError, ValueError)):
        study.submit(service, spec, planned["plan_hash"])


def test_grid_seeds_have_independent_owned_pairs_and_force_creates_new_attempt(experiment):
    service, spec = experiment
    spec["noise_enabled"] = True
    spec["seeds"] = [3, 7]
    spec["axes"] = [
        {"parameter": "electrical_control_policy.latency.actuator_ns", "values": [0, 90_000_000]}
    ]
    proposal = study.plan(service, spec)
    parent = study.submit(service, spec, proposal["plan_hash"])
    assert len(parent["children"]) == 8
    another = study.submit(service, {**spec, "force": True}, proposal["plan_hash"])
    assert another["id"] != parent["id"]
    assert not {c["run_id"] for c in parent["children"]} & {
        c["run_id"] for c in another["children"]
    }
    study.cancel(service, another["id"])
    assert all(
        service.registry.get_run(c["run_id"])["status"] == "cancelled" for c in another["children"]
    )
    finish_all(service)
    result = study.get(service, parent["id"])["result"]["summary"]
    assert result["complete_pair_count"] == 4
    for cell in result["cells"]:
        baseline = cell["baseline"]["actual_timing"]
        candidate = cell["candidate"]["actual_timing"]
        if cell["coordinates"]["electrical_control_policy.latency.actuator_ns"]:
            assert [f["applied_exposure_ns"] for f in baseline] == [
                f["applied_exposure_ns"] for f in candidate
            ]
        else:
            assert baseline[1]["applied_exposure_ns"] != candidate[1]["applied_exposure_ns"]


def test_plan_hash_change_requires_reapproval_without_writes(experiment):
    service, spec = experiment
    proposal = study.plan(service, spec)
    before = snapshot(service)
    spec["evaluation"]["hold"]["consecutive_observations"] += 1
    with pytest.raises(ConflictError):
        study.submit(service, spec, proposal["plan_hash"])
    assert snapshot(service) == before


@pytest.mark.parametrize("all_failed", [False, True])
def test_durable_partial_and_failed_pair_evidence_never_invent_raw(experiment, all_failed):
    service, spec = experiment
    spec["seeds"] = [3] if all_failed else [3, 7]
    proposal = study.plan(service, spec)
    parent = study.submit(service, spec, proposal["plan_hash"])
    finished = []
    while job := service.registry.claim():
        owner = job.get("electrical_response_owner")
        if owner and (all_failed or owner["cell_id"] == "cell-000"):
            service.registry.finish(job["id"], error="Deliberate fixture capture failure")
            continue
        try:
            result = service.execute_run(job)
            finished.append(service.registry.finish(job["id"], result=result))
        except study.NoCompletePairsError as error:
            assert all_failed and job["id"] == parent["id"]
            finished.append(
                service.registry.finish(job["id"], result=error.evidence_result, error=str(error))
            )
    record = study.get(service, parent["id"])
    summary = record["result"]["summary"]
    assert record["status"] == ("failed" if all_failed else "succeeded")
    assert summary["status"] == ("no_complete_pairs" if all_failed else "partial")
    assert summary["complete_pair_count"] == (0 if all_failed else 1)
    failed = summary["cells"][0]
    assert failed["comparison"]["status"] == "missing"
    assert all(
        failed[role]["raw_artifact"] is None and failed[role]["evaluation"] is None
        for role in ("baseline", "candidate")
    )
    assert study.export_manifest(service, parent["id"])["study"]["status"] == record["status"]


def test_aggregate_passes_only_original_raw_format_and_minimal_timing_to_evaluator(
    experiment, monkeypatch
):
    service, spec = experiment
    proposal = study.plan(service, spec)
    parent = study.submit(service, spec, proposal["plan_hash"])
    evaluate = study.electrical_response.evaluate
    observed = []

    def checked(raw, raw_format, frames, evaluation):
        assert raw.ndim == 4 and raw.shape[1] == 1 and raw.dtype.kind in "iu"
        assert all(
            set(frame)
            == {
                "frame_id",
                "start_ns",
                "applied_exposure_ns",
                "row_start_ns",
                "row_end_ns",
                "statistics_ready_ns",
            }
            for frame in frames
        )
        assert evaluation == spec["evaluation"]
        observed.append(hashlib.sha256(raw.tobytes()).hexdigest())
        return evaluate(raw, raw_format, frames, evaluation)

    monkeypatch.setattr(study.electrical_response, "evaluate", checked)
    finish_all(service)
    assert len(observed) == 2 and observed[0] != observed[1]
    study.get(service, parent["id"])
    assert len(observed) == 4


@pytest.mark.parametrize(
    "change", ["duplicate_axis", "over_budget", "phase_without_pwm", "bool_seed", "clock", "roi"]
)
def test_whole_grid_invalid_spec_refuses_without_publishing(experiment, change):
    service, spec = experiment
    before = snapshot(service)
    if change == "duplicate_axis":
        spec["axes"] = [
            {"parameter": "electrical_control_policy.latency.actuator_ns", "values": [0]}
        ] * 2
    elif change == "over_budget":
        spec["seeds"] = list(range(13))
    elif change == "phase_without_pwm":
        spec["axes"] = [{"parameter": "waveform.phase_ns", "values": [0]}]
    elif change == "bool_seed":
        spec["seeds"] = [True]
    elif change == "clock":
        spec["evaluation"]["clock"]["clock_id"] = "unbound"
    else:
        spec["evaluation"]["roi"]["shape"] = [100, 100]
    with pytest.raises(ValueError):
        study.plan(service, spec)
    assert snapshot(service) == before


def test_two_axis_pwm_phase_and_latency_grid_runs_actual_matched_captures(experiment):
    service, spec = experiment
    original = service.registry.get("capture_programs", spec["capture_program_id"])
    data = {k: deepcopy(v) for k, v in original.items() if k in CaptureProgramCreate.model_fields}
    data.update(revision="study-pwm", parents=[original["id"]])
    data["payload"]["waveform"] = {
        "schema": "camera_light_waveform_v1",
        "kind": "pwm",
        "clock_id": spec["evaluation"]["clock"]["clock_id"],
        "period_ns": 731_000,
        "on_time_ns": 173_000,
        "phase_origin_ns": 0,
        "low": 0,
        "high": 1,
    }
    program = study.capture_programs.create(service, data)
    spec["capture_program_id"] = program["id"]
    spec["evaluation"]["stimulus"]["kind"] = "pwm"
    spec["axes"] = [
        {"parameter": "waveform.phase_ns", "values": [0, 317_000]},
        {"parameter": "electrical_control_policy.latency.controller_ns", "values": [0, 90_000_000]},
    ]
    proposal = study.plan(service, spec)
    parent = study.submit(service, spec, proposal["plan_hash"])
    finish_all(service)
    result = study.get(service, parent["id"])["result"]["summary"]
    assert result["complete_pair_count"] == 4
    selected = [
        c
        for c in result["cells"]
        if c["coordinates"]["electrical_control_policy.latency.controller_ns"] == 0
    ]
    raw = [
        load_array(service, service.registry.get_run(c["baseline"]["run_id"]), "raw_groups")
        for c in selected
    ]
    assert not np.array_equal(raw[0], raw[1])
    assert all(
        c[role]["evaluation"]["settling"]["status"] == "not_applicable"
        for c in result["cells"]
        for role in ("baseline", "candidate")
    )


def test_actual_varying_scene_cannot_be_declared_as_constant_step(experiment):
    service, spec = experiment
    scene = study.sequences.create_fixture(
        service,
        study.sequences.fixture_template(service, spec["assembly_id"])
        | {"shape": [16, 16], "knot_count": 2, "pattern": "moving_spot"},
    )
    original = service.registry.get("control_sequences", spec["control_sequence_id"])
    data = {k: deepcopy(v) for k, v in original.items() if k in ControlSequenceCreate.model_fields}
    data.update(revision="varying-source", scene_sequence_id=scene["id"])
    control = study.sequences.create_sequence(service, "control_sequences", data)
    spec.update(scene_sequence_id=scene["id"], control_sequence_id=control["id"])
    before = snapshot(service)
    with pytest.raises(ValueError, match="post-change irradiance varies"):
        study.plan(service, spec)
    with pytest.raises(ValueError, match="post-change irradiance varies"):
        study.template(
            service, spec["assembly_id"], scene["id"], control["id"], spec["capture_program_id"]
        )
    assert snapshot(service) == before


def test_nonlinear_pair_retains_original_format_and_code_only_evaluation(experiment):
    from camera_platform import nonlinear_examples

    service, spec = experiment
    original = service.registry.get("capture_programs", spec["capture_program_id"])
    data = {k: deepcopy(v) for k, v in original.items() if k in CaptureProgramCreate.model_fields}
    data.update(revision="study-nonlinear", parents=[original["id"]])
    data["payload"]["electrical_model"] = nonlinear_examples.single_model(
        original["sensor_model_snapshot"]["payload"]["readout"]
    )
    program = study.capture_programs.create(service, data)
    spec["capture_program_id"] = program["id"]
    proposal = study.plan(service, spec)
    parent = study.submit(service, spec, proposal["plan_hash"])
    finish_all(service)
    cell = study.get(service, parent["id"])["result"]["summary"]["cells"][0]
    for role in ("baseline", "candidate"):
        branch = cell[role]
        assert branch["raw_format"]["response_type"] == "nonlinear_charge_voltage"
        assert branch["evaluation"]["raw_format"] == branch["raw_format"]
