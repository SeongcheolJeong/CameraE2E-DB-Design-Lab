"""Explicit immutable publication of an anchored fitting result, never approval."""

from __future__ import annotations

from copy import deepcopy

from . import readout_calibrations, readout_fits
from .readout_calibration_contracts import FIELDS, validate_metadata
from .readout_fit_contracts import ReadoutFitPublish
from .registry import ConflictError, IntegrityError, digest

SCHEMA = "camera_fitted_response_calibration_v1"


def _document(job, publication):
    if job["status"] != "succeeded" or job.get("result") is None:
        raise ConflictError("Only a succeeded response fit has a publishable candidate")
    if publication["request_sha256"] != job["content_hash"] or (
        publication["result_sha256"] != digest(job["result"])
    ):
        raise ConflictError("Publication request/result hashes differ from the saved fit")
    result, base = job["result"], job["base_calibration_snapshot"]
    if result["optimizer_report"]["converged"] is not True:
        raise ConflictError("A nonconverged response fit cannot publish a usable calibration")
    data = {key: deepcopy(base[key]) for key in FIELDS if key in base}
    training, validation = job["training_observations"], job["validation_observations"]
    data.update(
        schema=SCHEMA, name=publication["name"], revision=publication["revision"],
        source_kind="estimated", source_uri="readout-fit://" + job["id"],
        method_and_version=readout_fits.PLUGIN_ID, parents=[base["id"]],
        dataset_role="development",
        dataset_ids=sorted({d for r in training for d in r["dataset_ids"]}),
        reference_capture_ids=sorted({r["capture_id"] for r in training}),
        electrical_model=deepcopy(result["candidate"]["electrical_model"]),
        applicability=deepcopy(result["candidate"]["applicability"]),
        charge_reference=deepcopy(training[0]["charge_reference"]),
        instrument=deepcopy(training[0]["charge_reference"]["instrument"]),
        uncertainty={"kind": "unknown", "reason": (
            "Conditional response/noise estimates; "
            "external charge uncertainty retained separately; "
            "total physical and post-ISP uncertainty not established")},
        fit_job_id=job["id"], request_sha256=job["content_hash"], result_sha256=digest(result),
        candidate_sha256=result["candidate"]["candidate_sha256"],
        publication_request=deepcopy(publication),
        training_observation_sha256={r["id"]: digest(r) for r in training},
        evaluation_observation_sha256={r["id"]: digest(r) for r in validation},
        training_tokens=deepcopy(job["training_tokens"]),
        evaluation_tokens=deepcopy(job["evaluation_tokens"]),
        evaluation_use="held_out_of_optimizer_but_used_for_calibration_development",
        physical_validation="not_established", approval_status="draft",
        authority=deepcopy(readout_fits.AUTHORITY),
        interpretation="fitted_conditional_quantized_poisson_response_no_authentication",
        sensor_model_snapshot=deepcopy(base["sensor_model_snapshot"]),
        sensor_model_sha256=base["sensor_model_sha256"],
        parent_calibration_sha256={base["id"]: digest(base)},
        dark_rate=deepcopy(base["dark_rate"]),
        raw_formats=deepcopy(base["raw_formats"]), normalization_e=base["normalization_e"],
    )
    data["electrical_model_sha256"] = digest(data["electrical_model"])
    data["reference_capture_sha256"] = {
        r["capture_id"]: r["capture_sha256"] for r in training
    }
    dark = next(a for a in base["artifacts"] if a["name"] == "expected-dark-rate.npy")
    data["artifacts"] = [deepcopy(dark), *deepcopy(result["artifacts"])]
    # Share the strict physical/common contract, while retaining distinct fitted identity.
    normalized = validate_metadata({**{k: data[k] for k in FIELDS},
                                    "schema": "camera_nonlinear_response_calibration_v1"})
    if any(data[k] != value for k, value in normalized.items() if k != "schema"):
        raise IntegrityError("Fitted candidate violates the normalized response contract")
    return data


def _identity(job, publication):
    return "readout_calibration-" + digest({"fit_job_id": job["id"], **publication})


def publish(service, identity, payload):
    publication = ReadoutFitPublish.model_validate(payload).model_dump()
    job = readout_fits.get_job(service, identity)
    document = _document(job, publication)
    refs = [(r["kind"], r["snapshot"]) for r in job["references"]]
    return service.registry.publish_readout_calibration(
        document, refs, job, identity=_identity(job, publication),
        ancestor_jobs=job["evidence_closure"]["base"].get("readout_fit_jobs", []),
    )


def verify(service, record, state, active):
    """Participate in the existing bounded response ancestry traversal."""
    identity = record["id"]
    if identity in active or record.get("schema") != SCHEMA or len(record["parents"]) != 1:
        raise IntegrityError("Fitted response ancestry or schema differs")
    base_id = record["parents"][0]
    if base_id == identity:
        raise IntegrityError("Fitted response cannot be its own base")
    base = readout_calibrations._tree(service, base_id, state, active | {identity})
    job = readout_fits.get_job(service, record["fit_job_id"], verify=False)
    if job["base_calibration_snapshot"] != base:
        raise IntegrityError("Fitted calibration no longer matches its exact base")
    readout_fits.verify_inputs(service, job, base=base)
    publication = ReadoutFitPublish.model_validate(record["publication_request"]).model_dump()
    expected = _document(job, publication)
    actual = {k: v for k, v in record.items() if k not in {"id", "created_at", "content_hash"}}
    if actual != expected or identity != _identity(job, publication):
        raise IntegrityError("Fitted response differs from the frozen candidate/publication")
    for key in ("observations", "fit_jobs", "evaluation_captures"):
        state.setdefault(key, {})
    for key in ("decoder_evaluation_tokens", "extra_fitting_tokens"):
        state.setdefault(key, set())
    state["fit_jobs"][job["id"]] = job
    for role, target in (("training", "captures"), ("validation", "evaluation_captures")):
        closure = job["evidence_closure"][role]
        state["observations"].update({r["id"]: r for r in closure["observations"]})
        state[target].update({r["id"]: r for r in closure["captures"]})
    state["extra_fitting_tokens"].update(job["training_tokens"])
    state["decoder_evaluation_tokens"].update(job["evaluation_tokens"])
    state["records"][identity] = record
    state["bytes"] += sum(a["bytes"] for a in job["result"]["artifacts"])
    # Count original RAW/observation artifacts too, including all fitted ancestors.
    artifacts = readout_fits._unique_artifacts([
        *state["records"].values(), *state["captures"].values(),
        *state["observations"].values(), *state["evaluation_captures"].values(),
        *state["fit_jobs"].values(),
    ])
    closure_bytes = sum({a["sha256"]: a["bytes"] for a in artifacts}.values())
    if (state["bytes"] > readout_calibrations.MAX_CLOSURE_BYTES
            or closure_bytes > readout_calibrations.MAX_CLOSURE_BYTES
            or len(state["captures"]) + len(state["evaluation_captures"]) > 100
            or len(state["observations"]) > 100 or len(state["fit_jobs"]) > 100):
        raise IntegrityError("Fitted response lineage exceeds the bounded evidence closure")
    return record


verify_tree = verify
