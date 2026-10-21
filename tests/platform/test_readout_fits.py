"""Original authored RAW → frozen fitting → explicit immutable calibration."""

import io
import json
from copy import deepcopy

import numpy as np
import pytest
from test_nonlinear_captures import capture_service as capture_service
from test_nonlinear_captures import metadata as capture_metadata
from test_readout_fit_math import paired_model, sampled_groups, single_model

from camera_platform import (
    nonlinear_captures,
    readout_calibrations,
    readout_fits,
    readout_fitted_calibrations,
    readout_observations,
)
from camera_platform.evidence_package import read_bundle
from camera_platform.registry import ConflictError, IntegrityError, digest


def npy(value):
    stream = io.BytesIO()
    np.save(stream, value, allow_pickle=False)
    return stream.getvalue()


def build_fixture(capture_service, *, shared=False, dark_rate=0.0, means=(0, 4, 12, 24)):
    """Uses independent scalar Poisson/voltage/nearest-even forward code authoring."""
    service, assembly, test = capture_service
    registry = service.registry
    sensor = next(registry.get("models", i) for i in assembly["model_ids"]
                  if registry.get("models", i)["kind"] == "sensor")
    body = {k: deepcopy(v) for k, v in sensor.items()
            if k not in {"id", "created_at", "content_hash"}}
    body["payload"]["readout"] = {
        "full_well_e": 32.0, "read_noise_e": 1.0, "dark_current_e_per_s": 1.0,
        "gain": 1.0, "bit_depth": 10, "black_level_dn": 128.0,
    }
    sensor = registry.put("models", body)
    model_ids = [sensor["id"] if registry.get("models", i)["kind"] == "sensor" else i
                 for i in assembly["model_ids"]]
    body = {k: deepcopy(v) for k, v in assembly.items()
            if k not in {"id", "created_at", "content_hash"}}
    body["model_ids"] = model_ids
    assembly = registry.put("assemblies", body)
    mode = "shared_charge_paths" if shared else "sequential_frames"
    truth = paired_model() if shared else single_model()
    data = readout_calibrations.template(service, sensor["id"], mode)
    data["electrical_model"] = truth
    data["applicability"]["readouts"] = [{
        "readout_id": ["HCG", "LCG"][i] if shared else "single",
        "analog_gain": [p["analog_gain"]] * 2 if shared else [1.5, 1.5],
        "digital_gain": p["digital_gain"],
    } for i, p in enumerate(truth["paths"] if shared else [truth])]
    base = readout_calibrations.import_calibration(
        service, data, npy(np.full(data["dark_rate_shape"], dark_rate)),
        json.dumps(readout_calibrations.evidence_template(data)).encode(),
    )
    records = []
    for label, seed, role in (("train", 872, "development"), ("heldout", 994, "holdout")):
        groups = sampled_groups(truth, means, count=64, seed=seed, analog_gain=1.5)
        raw = np.stack([g["raw_dn"].T.reshape((-1, 8, 8)) for g in groups])
        if not shared:
            raw = raw[:, 0]
        meta = capture_metadata(service, assembly, test, mode, suffix=label, count=4)
        meta["raw_formats"] = deepcopy(base["raw_formats"])
        for frame in meta["frames"]:
            frame["applied"]["gain"] = 1.0 if shared else 1.5
            frame["applied"]["readout_settings"] = [{
                "analog_gain": d["analog_gain"][0], "digital_gain": d["digital_gain"],
            } for d in base["applicability"]["readouts"]]
        capture = nonlinear_captures.import_capture(service, meta, npy(raw.astype(np.uint16)))
        obs = readout_observations.template(service, capture["id"])
        obs.update(source_kind="simulated", dataset_ids=["anchor-" + label], dataset_role=role,
                   charge_reference=deepcopy(base["charge_reference"]),
                   roi={"row_range": [2, 10], "column_range": [4, 12]})
        for frame, group in zip(obs["frames"], groups, strict=True):
            frame["mean_total_charge_e"] = [group["mean_charge_e"]] * 4
        records.append(readout_observations.import_observations(
            service, obs, json.dumps(readout_observations.evidence_template(obs)).encode()))
    spec = {
        "schema": "camera_readout_fit_request_v1", "name": "Authored response fitting",
        "base_calibration_id": base["id"], "training_observation_ids": [records[0]["id"]],
        "validation_observation_ids": [records[1]["id"]], "slope_scale_bounds": [0.5, 1.5],
        "sigma_v_bounds": [0.001, 0.03], "max_iterations": 100,
    }
    return service, base, records, spec


@pytest.fixture
def fit_service(capture_service, monkeypatch):
    from camera_platform import service as service_module

    environment = {"schema": "pure-test-frozen-environment", "version": "fixture"}
    monkeypatch.setattr(service_module, "environment_fingerprint", lambda: deepcopy(environment))
    capture_service[0].loaded_environment = deepcopy(environment)
    return capture_service


def complete(service, spec):
    plan = readout_fits.plan(service, spec)
    job = readout_fits.submit(service, spec, plan_hash=plan["plan_hash"])
    claimed = service.registry.claim()
    assert claimed["id"] == job["id"]
    result = readout_fits.execute(service, claimed)
    service.registry.finish(job["id"], result=result)
    return readout_fits.get_job(service, job["id"])


def publication(job):
    return {"name": "Authored fitted response", "revision": "fit-1",
            "request_sha256": job["content_hash"], "result_sha256": digest(job["result"])}


def physical_rows(service):
    with service.registry.connection() as conn:
        return {name: [tuple(row) for row in conn.execute("SELECT * FROM " + name)]
                for name in ("records", "jobs", "audit", "artifact_owners")}


@pytest.mark.parametrize("shared", [False, True])
def test_actual_fit_publication_replayable_closure_and_bundle(fit_service, shared):
    service, base, records, spec = build_fixture(fit_service, shared=shared)
    before = physical_rows(service)
    plan = readout_fits.plan(service, spec)
    assert physical_rows(service) == before
    assert "maximum_optimizer_likelihood_terms" in plan["resources"]["training"]
    assert not set(plan["training_tokens"]) & set(plan["evaluation_tokens"])
    assert plan["applicability"]["exposure_s"] == [0.004, 0.004]
    job = complete(service, spec)
    assert job["result"]["optimizer_report"]["converged"]
    assert len(service.registry.list("readout_calibrations")) == 1
    assert readout_fits.submit(service, spec, plan_hash=plan["plan_hash"])["id"] == job["id"]
    fitted = readout_fitted_calibrations.publish(service, job["id"], publication(job))
    after = physical_rows(service)
    assert readout_fitted_calibrations.publish(service, job["id"], publication(job)) == fitted
    assert physical_rows(service) == after
    assert readout_calibrations.get(service, base["id"]) == base
    assert readout_calibrations.get(service, fitted["id"]) == fitted
    lineage = readout_calibrations.lineage(service, fitted)
    assert records[0]["capture_id"] in {r["id"] for r in lineage["reference_captures"]}
    assert records[1]["capture_id"] not in {r["id"] for r in lineage["reference_captures"]}
    assert records[1]["capture_id"] in {r["id"] for r in lineage["evaluation_captures"]}
    assert "capture-id:" + records[1]["capture_id"] in lineage["decoder_evaluation_tokens"]
    assert "capture-id:" + records[1]["capture_id"] not in lineage["decoder_fitting_tokens"]
    assert fitted["reference_capture_ids"] == [records[0]["capture_id"]]
    prepared = readout_calibrations.prepare_for_capture(
        service, fitted["id"], records[1]["capture_snapshot"])
    assert prepared["normalization_e"] == base["normalization_e"]
    assert prepared["dark_e"].shape == (4, 8, 8)
    manifest = readout_fits.export(service, job["id"])
    assert manifest["publication_binding"]["result_sha256"] == digest(job["result"])
    path = readout_fits.bundle(service, job["id"])
    assert read_bundle(path, expected_manifest_sha256=digest(manifest)) == manifest
    assert fitted["authority"] == {"physical_validation": False, "release": False}


def test_roles_same_raw_alias_and_no_rows_on_refusal(fit_service):
    service, base, records, spec = build_fixture(fit_service)
    before = physical_rows(service)
    invalid = deepcopy(spec)
    invalid["training_observation_ids"], invalid["validation_observation_ids"] = (
        invalid["validation_observation_ids"], invalid["training_observation_ids"])
    with pytest.raises(ValueError, match="roles"):
        readout_fits.plan(service, invalid)
    assert physical_rows(service) == before
    source = records[0]
    from camera_platform.readout_fit_contracts import FIELDS

    metadata = {k: deepcopy(source[k]) for k in FIELDS}
    metadata.update(name="same exposure new target identity", dataset_role="holdout",
                    dataset_ids=["relabeled-holdout"])
    alias = readout_observations.import_observations(
        service, metadata, json.dumps(readout_observations.evidence_template(metadata)).encode())
    invalid = deepcopy(spec)
    invalid["validation_observation_ids"] = [alias["id"]]
    before = physical_rows(service)
    with pytest.raises(ValueError, match="overlap"):
        readout_fits.plan(service, invalid)
    assert physical_rows(service) == before


def test_preflight_limits_hash_pin_and_environment_refusal(fit_service, monkeypatch):
    service, base, records, spec = build_fixture(fit_service)
    before = physical_rows(service)
    with pytest.raises(ConflictError, match="plan changed"):
        readout_fits.submit(service, spec, plan_hash="0" * 64)
    monkeypatch.setattr(readout_fits.readout_fit_math, "MAX_OBSERVATIONS", 200)
    with pytest.raises(ValueError, match="samples"):
        readout_fits.plan(service, spec)
    assert physical_rows(service) == before
    service.loaded_environment = {"changed": True}
    with pytest.raises(ConflictError, match="source changed"):
        readout_fits.plan(service, spec)


def test_successful_read_does_not_cache_corrupt_original_or_diagnostics(fit_service):
    service, base, records, spec = build_fixture(fit_service)
    job = complete(service, spec)
    fitted = readout_fitted_calibrations.publish(service, job["id"], publication(job))
    assert readout_calibrations.get(service, fitted["id"]) == fitted
    artifact = job["result"]["artifacts"][0]
    path = service.registry.blob_path(artifact["sha256"])
    original = path.read_bytes()
    path.write_bytes(b"!" + original[1:])
    with pytest.raises(IntegrityError):
        readout_fits.get_job(service, job["id"])
    with pytest.raises(IntegrityError):
        readout_calibrations.get(service, fitted["id"])


def test_publish_cannot_use_running_cancelled_or_changed_result(fit_service):
    service, base, records, spec = build_fixture(fit_service)
    plan = readout_fits.plan(service, spec)
    job = readout_fits.submit(service, spec, plan_hash=plan["plan_hash"])
    payload = {"name": "no candidate", "revision": "0", "request_sha256": job["content_hash"],
               "result_sha256": "0" * 64}
    with pytest.raises(ConflictError, match="succeeded"):
        readout_fitted_calibrations.publish(service, job["id"], payload)
    claimed = service.registry.claim()
    result = readout_fits.execute(service, claimed)
    service.registry.cancel(job["id"])
    service.registry.finish(job["id"], result=result)
    with pytest.raises(ConflictError, match="succeeded"):
        readout_fitted_calibrations.publish(service, job["id"], payload)
    assert len(service.registry.list("readout_calibrations")) == 1


def test_historical_job_reads_ignore_current_environment_drift(fit_service):
    service, base, records, spec = build_fixture(fit_service)
    job = complete(service, spec)
    service.loaded_environment = {"new source": "must not run old job"}
    assert readout_fits.get_job(service, job["id"]) == job
    with pytest.raises(ConflictError):
        readout_fits.execute(service, job)


@pytest.mark.parametrize("change", ["electronics", "nonconvergence", "closure"])
def test_manually_submitted_false_candidate_or_closure_cannot_publish(fit_service, change):
    service, base, records, spec = build_fixture(fit_service)
    original = complete(service, spec)
    frozen = {k: deepcopy(v) for k, v in original.items() if k not in {
        "id", "status", "cache_key", "content_hash", "created_at", "updated_at", "result", "error",
        "artifacts", "cache_hit",
    }}
    result = deepcopy(original["result"])
    if change == "closure":
        frozen["evidence_closure"]["training"]["captures"] = []
        frozen["plan_hash"] = digest({k: v for k, v in frozen.items()
                                     if k not in {"plan_hash", "job_kind", "plugin_id"}})
    elif change == "electronics":
        candidate = result["candidate"]
        candidate["electrical_model"]["adc"]["full_scale_v"] *= 1.01
        candidate["candidate_sha256"] = digest({k: v for k, v in candidate.items()
                                                if k != "candidate_sha256"})
        result["optimizer_report"]["candidate_model_sha256"] = digest(candidate["electrical_model"])
    else:
        result["optimizer_report"]["converged"] = False
        for path in result["optimizer_report"]["paths"]:
            path["converged"] = False
    for artifact, key in zip(result["artifacts"], (
        "candidate", "optimizer_report", "training_report", "validation_report"
    ), strict=True):
        replacement = readout_fits._json_artifact(service, artifact["name"], result[key])
        artifact.clear()
        artifact.update(replacement)
    job = service.registry.submit(frozen, digest(frozen), force=True)
    service.registry.claim()
    job = service.registry.finish(job["id"], result=result)
    with pytest.raises((ConflictError, IntegrityError), match={
        "electronics": "frozen electronics", "nonconvergence": "nonconverged",
        "closure": "closure",
    }[change]):
        readout_fitted_calibrations.publish(service, job["id"], publication(job))
    assert len(service.registry.list("readout_calibrations")) == 1


def test_heldout_groups_load_only_after_candidate_is_frozen(fit_service, monkeypatch):
    service, base, records, spec = build_fixture(fit_service)
    planned = readout_fits.plan(service, spec)
    job = readout_fits.submit(service, spec, plan_hash=planned["plan_hash"])
    job = service.registry.claim()
    events = []
    original_groups, original_fit = readout_fits._groups, readout_fits.readout_fit_math.fit

    def groups(service, selected):
        events.append(selected[0]["dataset_role"])
        return original_groups(service, selected)

    def fit(*args, **kwargs):
        assert events == ["development"]
        result = original_fit(*args, **kwargs)
        events.append("candidate_returned")
        return result

    monkeypatch.setattr(readout_fits, "_groups", groups)
    monkeypatch.setattr(readout_fits.readout_fit_math, "fit", fit)
    result = readout_fits.execute(service, job)
    assert events == ["development", "candidate_returned", "holdout"]
    assert result["candidate_frozen_before_evaluation"] is True


def test_total_charge_cannot_imply_negative_photocharge_and_dark_is_not_added(fit_service):
    service, base, records, spec = build_fixture(fit_service, dark_rate=2.0)
    before = physical_rows(service)
    with pytest.raises(ValueError, match="below expected dark"):
        readout_fits.plan(service, spec)
    assert physical_rows(service) == before
    # 2 e/s × .004 s = .008 e is already part of the independently authored total.
    from camera_platform.readout_fit_contracts import FIELDS

    corrected = []
    for record in records:
        metadata = {key: deepcopy(record[key]) for key in FIELDS}
        metadata["frames"][0]["mean_total_charge_e"] = [0.008] * 4
        metadata["dataset_ids"] = ["positive-" + d for d in metadata["dataset_ids"]]
        evidence = json.dumps(readout_observations.evidence_template(metadata)).encode()
        corrected.append(readout_observations.import_observations(service, metadata, evidence))
    records = corrected
    spec.update(training_observation_ids=[records[0]["id"]],
                validation_observation_ids=[records[1]["id"]])
    planned = readout_fits.plan(service, spec)
    actual = readout_fits._groups(service, [records[0]])
    assert actual[0]["mean_charge_e"] == 0.008
    resources = readout_fits.readout_fit_math.preflight(
        planned["fitting_model"], actual, analog_gain=planned["analog_gain"])
    assert resources["raw_input_sha256"] == planned["resources"]["training"]["raw_input_sha256"]
