"""Actual retained RAW planning, atomic owned cells, metric evidence and holdout boundaries."""

import io
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from test_isp_calibration import source as source

from camera_platform import imaging, isp_calibration, isp_replay_v2
from camera_platform import isp_studies as studies
from camera_platform.registry import ConflictError, IntegrityError
from camera_platform.worker import execute


def acquire(service, assembly_id, seed=31, frames=4, scene=None):
    if scene is None:
        fixture = imaging.fixture_template(service, assembly_id)
        fixture.update(shape=[16, 20], pattern="flat")
        scene = imaging.create_fixture(service, fixture)
    test = service.create_test(imaging.spatial_test_template(service, assembly_id, scene["id"]))
    queued = service.submit_run(
        {
            "assembly_id": assembly_id,
            "test_id": test["id"],
            "scene_id": scene["id"],
            "plugin_id": "spatial_scene_v1",
            "parameters": {"seed": seed, "frames": frames},
        }
    )
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    run = service.registry.finish(job["id"], result=service.execute_run(job))
    return imaging.replay_source(service, "run", run["id"]), scene


@pytest.fixture
def prepared(source):
    service, old = source
    raw, scene = acquire(service, old["source_snapshot"]["assembly_id"])
    calibration = isp_calibration.import_calibration(
        service, isp_calibration.template(service, "run", raw["source_id"])
    )
    model = isp_replay_v2.template(
        service, source_kind="run", source_id=raw["source_id"], calibration_id=calibration["id"]
    )
    model["payload"]["raw_denoise"]["strength"] = 0.0
    model["payload"]["linear_sharpen"].update(mode="bounded_detail_v1", amount=0.0)
    baseline = service.create_model(model)
    spec = studies.template(service, "run", raw["source_id"], calibration["id"], baseline["id"])
    spec["axes"] = [{"parameter": "raw_denoise.strength", "values": [0.0, 0.8]}]
    return service, raw, scene, calibration, baseline, spec


def rows(service):
    with service.registry.connection() as conn:
        return {
            table: [tuple(r) for r in conn.execute("SELECT * FROM " + table)]
            for table in ("records", "jobs", "audit")
        }


def submit(prepared):
    service, *_, spec = prepared
    plan = studies.plan(service, spec)
    return studies.submit(service, {"specification": spec, "plan_hash": plan["plan_hash"]})


def complete(service, study):
    for cell in study["cells"]:
        job = service.registry.claim()
        assert job["id"] == cell["job_id"]
        execute(service, job["id"])
        result = service.registry.get_run(job["id"])
        assert result["status"] == "succeeded", result["error"]
    return studies.analyze(service, study["id"])


def reference(service, raw, baseline):
    metadata = {
        "name": "Independent authored flat reference",
        "source_kind": "estimated",
        "source_uri": "fixture://independent-authored-flat",
        "license": "MIT",
        "method_and_version": "authored_truth_v1",
        "acquisition_group_id": "reference-flat",
        "derivation": "authored_independent",
        "geometry": raw["geometry"],
        "pixel_pitch_um": isp_calibration.template(service, "run", raw["source_id"])["binding"][
            "pixel_pitch_um"
        ],
        "common_linear_profile": {
            k: baseline["payload"][k] for k in ("black_level", "demosaic", "white_balance", "ccm")
        },
        "normalization_dn": 2 ** raw["raw_format"]["bit_depth"]
        - 1
        - raw["raw_format"]["black_level_dn"],
        "uncertainty": {"kind": "unknown", "reason": "Synthetic fixture only"},
        "description": "Declared scalar flat, not fitted to candidate output.",
    }
    stream = io.BytesIO()
    np.save(stream, np.full((*raw["raw_shape"][-2:], 3), 0.2), allow_pickle=False)
    return metadata, stream.getvalue()


def test_read_only_plan_then_atomic_idempotence_and_actual_worker_metrics(prepared):
    service, raw, _, _, _, spec = prepared
    before = rows(service)
    blobs = sorted(service.registry.blobs.rglob("*"))
    first = studies.plan(service, spec)
    second = studies.plan(service, spec)
    assert first["plan_hash"] == second["plan_hash"]
    assert rows(service) == before
    assert sorted(service.registry.blobs.rglob("*")) == blobs
    assert first["job_count"] == 2
    study = studies.submit(service, {"specification": spec, "plan_hash": first["plan_hash"]})
    assert len(study["run_ids"]) == 2
    assert (
        len(service.registry.list("models"))
        == len([r for r in before["records"] if r[0] == "models"]) + 1
    )
    incomplete = studies.analyze(service, study["id"])
    assert incomplete["status"] == "incomplete"
    assert incomplete["aggregate"]["pareto_provisional"]
    analysis = complete(service, study)
    assert analysis["status"] == "complete"
    assert analysis["coverage"]["succeeded"] == 2
    assert all(r["metrics"]["temporal_residual_std_dn"]["status"] == "ok" for r in analysis["rows"])
    assert all(r["metrics"]["mean_bias_dn"]["status"] == "unknown" for r in analysis["rows"])
    assert all(r["summary"]["raw_sha256"] == raw["raw_sha256"] for r in analysis["rows"])
    after = rows(service)
    assert (
        studies.submit(service, {"specification": spec, "plan_hash": first["plan_hash"]}) == study
    )
    assert rows(service) == after
    assert studies.analyze(service, study["id"]) == analysis
    manifest = studies.export_manifest(service, study["id"])
    assert studies.export_manifest(service, study["id"]) == manifest
    from camera_platform.evidence_package import read_bundle

    bundle = studies.export_bundle(service, study["id"])
    assert read_bundle(bundle) == manifest
    with zipfile.ZipFile(bundle) as archive:
        for artifact in manifest["artifacts"]:
            content = archive.read(f"artifacts/{artifact['sha256']}")
            import hashlib

            assert hashlib.sha256(content).hexdigest() == artifact["sha256"]


def test_worker_uses_signed_pretone_baseline_not_png_or_tone_output(prepared):
    from camera_platform.isp import default_profile, process_raw

    service, raw, _, _, baseline, _ = prepared
    study = submit(prepared)
    analysis = complete(service, study)
    job = studies.get_cell(service, study["cells"][0]["job_id"])
    measured = job["result"]["metric_result"]
    retained = studies._npy(service, measured["evidence"]["baseline_artifact"])
    profile = default_profile()
    for key in ("white_balance", "ccm", "tone"):
        profile[key] = baseline["payload"][key]
    expected = process_raw(
        studies._npy(service, raw["raw_artifact"]),
        raw["raw_format"],
        profile,
        geometry=raw["geometry"],
    )
    np.testing.assert_array_equal(retained, expected["arrays"]["ccm_rgb_frames"])
    assert not np.array_equal(retained, expected["arrays"]["rgb_frames"])
    assert analysis["physical_validation"] == "not_established"
    assert analysis["selection_authority"] is False


def test_explicit_selection_then_disjoint_fixed_validation_and_no_ranking(prepared):
    service, raw, _, _, _, spec = prepared
    study = submit(prepared)
    analysis = complete(service, study)
    body = {
        "candidate_id": study["candidates"][1]["candidate_id"],
        "rationale": "Finite synthetic tradeoff selected for a separate acquisition",
        "analysis_sha256": analysis["analysis_sha256"],
    }
    selected = studies.select_candidate(service, study["id"], body)
    assert studies.select_candidate(service, study["id"], body) == selected
    validation_raw, _ = acquire(service, raw["source_snapshot"]["assembly_id"], seed=77)
    validation = deepcopy(spec)
    validation.update(
        name="Independent synthetic validation",
        phase="independent_validation",
        axes=[],
        selection_id=selected["id"],
        parent_study_id=study["id"],
    )
    validation["datasets"][0].update(
        source_id=validation_raw["source_id"],
        purpose="independent_validation",
        acquisition_group_id="new-validation-acquisition",
    )
    plan = studies.plan(service, validation)
    assert plan["job_count"] == 2
    new = studies.submit(service, {"specification": validation, "plan_hash": plan["plan_hash"]})
    result = complete(service, new)
    assert all(r["pareto_status"] == "not_ranked_validation" for r in result["aggregate"]["rows"])
    with pytest.raises(ValueError, match="terminal tuning"):
        studies.select_candidate(
            service, new["id"], {**body, "analysis_sha256": result["analysis_sha256"]}
        )
    # New name/plan cannot recycle validation acquisition as another independent result.
    validation["name"] = "Relabelled holdout"
    with pytest.raises(ValueError, match="already used"):
        studies.plan(service, validation)


@pytest.mark.parametrize("fault", ["source", "scene", "group"])
def test_validation_rejects_relabelled_tuning_data(prepared, fault):
    service, raw, scene, _, _, spec = prepared
    study = submit(prepared)
    analysis = complete(service, study)
    selected = studies.select_candidate(
        service,
        study["id"],
        {
            "candidate_id": study["candidates"][0]["candidate_id"],
            "rationale": "Keep baseline",
            "analysis_sha256": analysis["analysis_sha256"],
        },
    )
    validation = deepcopy(spec)
    validation.update(
        phase="independent_validation",
        axes=[],
        selection_id=selected["id"],
        parent_study_id=study["id"],
    )
    validation["datasets"][0].update(
        purpose="independent_validation", acquisition_group_id="renamed"
    )
    if fault != "source":
        new, _ = acquire(
            service,
            raw["source_snapshot"]["assembly_id"],
            seed=77,
            scene=scene if fault == "scene" else None,
        )
        validation["datasets"][0]["source_id"] = new["source_id"]
    if fault == "group":
        validation["datasets"][0]["acquisition_group_id"] = spec["datasets"][0][
            "acquisition_group_id"
        ]
    with pytest.raises(ValueError, match="overlaps tuning"):
        studies.plan(service, validation)


@pytest.mark.parametrize("when", ["plan", "queued", "cached"])
@pytest.mark.parametrize("target", ["raw", "calibration", "reference"])
def test_missing_or_corrupt_inputs_refused_for_plan_worker_and_cache(prepared, when, target):
    service, raw, _, calibration, baseline, spec = prepared
    metadata, content = reference(service, raw, baseline)
    ref = studies.import_reference(service, metadata, content)
    spec["datasets"][0]["reference_id"] = ref["id"]
    study = submit(prepared) if when != "plan" else None
    if when == "cached":
        complete(service, study)
    descriptor = {
        "raw": raw["raw_artifact"],
        "calibration": calibration["artifacts"][0],
        "reference": ref["array_artifact"],
    }[target]
    path = service.registry.blob_path(descriptor["sha256"])
    saved = path.read_bytes()
    path.write_bytes(b"tampered evidence")
    try:
        if when == "queued":
            job = service.registry.claim()
            execute(service, job["id"])
            assert service.registry.get_run(job["id"])["status"] == "failed"
        else:
            with pytest.raises((ValueError, KeyError)):
                if when == "plan":
                    studies.plan(service, spec)
                else:
                    studies.get_study(service, study["id"])
    finally:
        path.write_bytes(saved)


def test_output_tamper_refused_by_analysis_cache_and_bundle(prepared):
    service, *_, spec = prepared
    study = submit(prepared)
    complete(service, study)
    job = studies.get_cell(service, study["cells"][0]["job_id"])
    descriptor = next(a for a in job["artifacts"] if a["name"] == "sharpened_rgb_frames.npy")
    path = service.registry.blob_path(descriptor["sha256"])
    saved = path.read_bytes()
    path.unlink()
    try:
        for operation in (
            lambda: studies.analyze(service, study["id"]),
            lambda: studies.submit(
                service, {"specification": spec, "plan_hash": study["plan_hash"]}
            ),
            lambda: studies.export_bundle(service, study["id"]),
        ):
            with pytest.raises((KeyError, ValueError)):
                operation()
    finally:
        path.write_bytes(saved)


def test_atomic_publication_rolls_back_all_models_jobs_and_audit(prepared, monkeypatch):
    service, *_, spec = prepared
    plan = studies.plan(service, spec)
    before = rows(service)
    original = service.registry._audit

    def refuse(conn, action, subject, details):
        if action == "runs.queued":
            raise RuntimeError("Injected publication failure after staged model insert")
        return original(conn, action, subject, details)

    monkeypatch.setattr(service.registry, "_audit", refuse)
    with pytest.raises(RuntimeError, match="Injected"):
        studies.submit(service, {"specification": spec, "plan_hash": plan["plan_hash"]})
    assert rows(service) == before


def test_cancel_owns_only_its_cells_preserves_success_and_restart(prepared):
    from camera_platform.service import PlatformService

    service, *_, spec = prepared
    study = submit(prepared)
    first = service.registry.claim()
    execute(service, first["id"])
    first = service.registry.get_run(first["id"])
    other = service.registry.submit({"name": "Unrelated owned job"}, "other-study-test")
    analysis = studies.cancel(service, study["id"])
    assert analysis["coverage"] == {
        "queued": 0,
        "running": 0,
        "succeeded": 1,
        "failed": 0,
        "cancelled": 1,
    }
    assert service.registry.get_run(first["id"]) == first
    assert service.registry.get_run(other["id"])["status"] == "queued"
    restarted = PlatformService(service.registry.directory, service.workspace)
    assert studies.analyze(restarted, study["id"]) == analysis
    assert (
        studies.submit(restarted, {"specification": spec, "plan_hash": study["plan_hash"]}) == study
    )


def test_plan_stale_environment_and_foreign_worker_request_rejected(prepared):
    service, *_, spec = prepared
    plan = studies.plan(service, spec)
    bad = deepcopy(spec)
    bad["question"] = "Different question"
    with pytest.raises(ConflictError, match="plan changed"):
        studies.submit(service, {"specification": bad, "plan_hash": plan["plan_hash"]})
    study = submit(prepared)
    job = service.registry.claim()
    changed = deepcopy(job)
    changed["replay_request"]["profile"]["raw_denoise"]["strength"] = 0.3
    with pytest.raises(IntegrityError, match="Worker cell differs"):
        studies.execute(service, changed)
    service.loaded_environment = {"changed": True}
    with pytest.raises((ConflictError, ValueError), match="changed|environment"):
        studies.execute(service, job)
    assert study["id"] == job["study_id"]


@pytest.mark.parametrize("fault", ["frames", "bypass", "jobs", "metric", "unit", "budget"])
def test_bounded_preflight_failures_are_write_free(prepared, fault, monkeypatch):
    service, raw, _, _, baseline, spec = prepared
    if fault == "frames":
        new, _ = acquire(service, raw["source_snapshot"]["assembly_id"], frames=2)
        spec["datasets"][0]["source_id"] = new["source_id"]
    elif fault == "bypass":
        from camera_platform.contracts import ModelCreate

        doc = {k: deepcopy(v) for k, v in baseline.items() if k in ModelCreate.model_fields}
        doc["payload"]["raw_denoise"]["mode"] = "bypass"
        spec["baseline_isp_model_id"] = service.create_model(doc)["id"]
    elif fault == "jobs":
        spec["axes"] = [
            {"parameter": "raw_denoise.strength", "values": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]},
            {"parameter": "linear_sharpen.amount", "values": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]},
        ]
    elif fault == "metric":
        spec["objectives"][0]["metric"] = "invented_snr"
    elif fault == "unit":
        spec["constraints"] = [
            {
                "metric": "temporal_residual_std_dn",
                "dataset_keys": ["flat"],
                "reducer": "max",
                "operator": "<=",
                "value": 4.0,
                "unit": "e-",
            }
        ]
    else:
        monkeypatch.setattr(studies, "MAX_ARCHIVE_BYTES", 10)
    before = rows(service)
    with pytest.raises(ValueError):
        studies.plan(service, spec)
    assert rows(service) == before


def test_reference_common_processing_binding_and_candidate_output_refusal(prepared):
    service, raw, _, _, baseline, spec = prepared
    metadata, content = reference(service, raw, baseline)
    metadata["common_linear_profile"]["white_balance"]["gains_rgb"][0] = 1.1
    ref = studies.import_reference(service, metadata, content)
    spec["datasets"][0]["reference_id"] = ref["id"]
    with pytest.raises(ValueError, match="WB/CCM"):
        studies.plan(service, spec)
    metadata["source_uri"] = "replay://candidate-output"
    with pytest.raises(ValueError, match="Candidate output"):
        studies.import_reference(service, metadata, content)


def test_fixed_acquisition_refuses_real_frame_setting_or_optical_changes(prepared):
    service, raw, scene, _, _, _ = prepared
    source = deepcopy(raw)
    source["frame_conditions"] = [
        {
            "frame_id": str(i),
            "frame_start_ns": i * 10_000_000,
            "applied_at_ns": i * 10_000_000,
            "exposure_ns": 2_000_000,
            "gain": 1.0,
        }
        for i in range(4)
    ]
    studies._fixed_acquisition(service, source)
    source["frame_conditions"][2]["exposure_ns"] *= 2
    with pytest.raises(ValueError, match="fixed exposure"):
        studies._fixed_acquisition(service, source)
    fixture = imaging.fixture_template(service, raw["source_snapshot"]["assembly_id"])
    fixture.update(shape=[16, 20], pattern="slanted_edge")
    changing = imaging.create_fixture(service, fixture)
    source = deepcopy(raw)
    source["source_snapshot"]["scene_sequence_snapshot"] = {
        "id": "declared-test-sequence",
        "payload": {"knots": [{"spectral_image_id": s["id"]} for s in [scene, changing]]},
    }
    with pytest.raises(ValueError, match="changing optical scene"):
        studies._fixed_acquisition(service, source)


def test_running_interruption_is_preserved_as_failed_new_attempt_is_explicit(prepared):
    service, *_, spec = prepared
    study = submit(prepared)
    job = service.registry.claim()
    service.registry.recover_interrupted()
    assert service.registry.get_run(job["id"])["status"] == "failed"
    assert (
        studies.submit(service, {"specification": spec, "plan_hash": study["plan_hash"]}) == study
    )
    next_spec = deepcopy(spec)
    next_spec.update(attempt=2, parent_study_id=study["id"])
    fresh = studies.plan(service, next_spec)
    assert fresh["plan_hash"] != study["plan_hash"]
    next_study = studies.submit(
        service, {"specification": next_spec, "plan_hash": fresh["plan_hash"]}
    )
    assert set(next_study["run_ids"]).isdisjoint(study["run_ids"])


def test_queue_capacity_is_checked_inside_all_or_nothing_transaction(prepared):
    service, *_, spec = prepared
    plan = studies.plan(service, spec)
    for i in range(63):
        service.registry.submit({"name": "Unrelated queue item " + str(i)}, "unrelated-" + str(i))
    before = rows(service)
    with pytest.raises(ConflictError, match="queue capacity"):
        studies.submit(service, {"specification": spec, "plan_hash": plan["plan_hash"]})
    assert rows(service) == before


def test_holdout_cannot_reuse_data_from_another_past_tuning_study(prepared):
    service, raw, _, _, _, spec = prepared
    first = submit(prepared)
    analysis = complete(service, first)
    selection = studies.select_candidate(
        service,
        first["id"],
        {
            "candidate_id": first["candidates"][0]["candidate_id"],
            "rationale": "Keep the baseline on unseen data",
            "analysis_sha256": analysis["analysis_sha256"],
        },
    )
    different, _ = acquire(service, raw["source_snapshot"]["assembly_id"], seed=82)
    other = deepcopy(spec)
    other["name"] = "Another earlier tuning experiment"
    other["datasets"][0].update(
        source_id=different["source_id"], acquisition_group_id="other-tuning"
    )
    planned = studies.plan(service, other)
    studies.submit(service, {"specification": other, "plan_hash": planned["plan_hash"]})
    validation = deepcopy(other)
    validation.update(
        phase="independent_validation",
        axes=[],
        selection_id=selection["id"],
        parent_study_id=first["id"],
    )
    validation["datasets"][0].update(
        purpose="independent_validation", acquisition_group_id="renamed-holdout"
    )
    with pytest.raises(ValueError, match="already used"):
        studies.plan(service, validation)


def test_damaged_input_does_not_prevent_owned_queue_cancellation(prepared):
    service, raw, *_ = prepared
    study = submit(prepared)
    path = service.registry.blob_path(raw["raw_sha256"])
    original = path.read_bytes()
    path.unlink()
    try:
        result = studies.cancel(service, study["id"])
        assert result["status"] == "cancellation_recorded_evidence_unavailable"
        assert all(
            service.registry.get_run(c["job_id"])["status"] == "cancelled" for c in study["cells"]
        )
    finally:
        path.write_bytes(original)


def test_fitting_acquisition_closure_is_transitive_and_registry_order_independent():
    from types import SimpleNamespace

    measurements = [
        {"id": "measurement-B", "dataset_ids": ["intermediate"], "raw_capture_id": "capture-B"},
        {"id": "measurement-A", "dataset_ids": ["fitting-set", "intermediate"]},
    ]
    capture = {
        "id": "capture-B",
        "dataset_ids": ["other-label"],
        "raw_frame_hashes": ["f" * 64],
        "artifacts": [{"sha256": "a" * 64}],
    }
    calibration = [{"dataset_ids": ["fitting-set"], "evidence_refs": []}]

    def result(records):
        service = SimpleNamespace(
            registry=SimpleNamespace(
                list=lambda kind: records if kind == "measurements" else [capture]
            )
        )
        return studies._fit_tokens(service, calibration)

    assert result(measurements) == result(list(reversed(measurements)))
    assert {"capture-B", "raw-frame-sha256:" + "f" * 64, "a" * 64} <= result(measurements)
