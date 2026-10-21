"""Bounded anchored response fitting with frozen inputs and explicit publication."""

from __future__ import annotations

import hashlib
from copy import deepcopy

import numpy as np

from . import readout_calibrations, readout_fit_math, readout_observations
from .readout_fit_contracts import ReadoutFitRequest
from .registry import ConflictError, IntegrityError, canonical, digest

JOB_KIND = "readout_fit"
PLUGIN_ID = "anchored_quantized_poisson_response_fit_v1"
MAX_INPUT_BYTES = 64 * 1024**2
MAX_RESULT_BYTES = 8 * 1024**2
MAX_RESULT_GRAPH_NODES = 4096
MAX_LOGICAL_BYTES = 64 * 1024**2
MAX_REFERENCES = 512
AUTHORITY = {"physical_validation": False, "release": False}


def _artifacts(value):
    if isinstance(value, dict):
        if {"name", "sha256", "bytes", "url"} <= value.keys():
            yield value
        else:
            for child in value.values():
                yield from _artifacts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _artifacts(child)


def _unique_artifacts(value):
    result = {}
    for item in _artifacts(value):
        key = item["sha256"], item["name"]
        if key in result and result[key] != item:
            raise IntegrityError("Conflicting original fitting artifact descriptors")
        result[key] = item
    return [result[key] for key in sorted(result)]


def _verify_artifacts(service, values):
    for artifact in _unique_artifacts(values):
        if (artifact.get("id") != artifact["sha256"]
                or artifact["url"] != "/api/v1/artifacts/" + artifact["sha256"]
                or type(artifact["bytes"]) is not int or artifact["bytes"] <= 0):
            raise IntegrityError("Invalid fitting artifact descriptor")
        content = service.registry.blob_path(artifact["sha256"]).read_bytes()
        if (len(content) != artifact["bytes"]
                or hashlib.sha256(content).hexdigest() != artifact["sha256"]):
            raise IntegrityError("Original fitting evidence artifact differs")


def _closure(service, records):
    merged = {"observations": {}, "captures": {}, "tokens": set()}
    for record in records:
        lineage = readout_observations.lineage(service, record)
        for key in ("observations", "captures"):
            for value in lineage[key]:
                merged[key][value["id"]] = value
        merged["tokens"].update(lineage["tokens"])
    return {key: sorted(value) if key == "tokens" else [value[k] for k in sorted(value)]
            for key, value in merged.items()}


def _references(base_lineage, training, validation):
    refs = {}

    def add(kind, record):
        key = kind, record["id"]
        if key in refs and refs[key] != record:
            raise IntegrityError("Conflicting fitting reference snapshots")
        refs[key] = record

    for record in base_lineage["calibrations"]:
        add("readout_calibrations", record)
        add("models", record["sensor_model_snapshot"])
    captures = [*base_lineage["reference_captures"],
                *base_lineage.get("evaluation_captures", [])]
    observations = list(base_lineage.get("readout_observations", []))
    for closure in (training, validation):
        captures.extend(closure["captures"])
        observations.extend(closure["observations"])
    for record in observations:
        add("readout_observations", record)
    for record in captures:
        add("captures", record)
        for model in record.get("model_snapshots", []):
            add("models", model)
        for field, kind in (("assembly_snapshot", "assemblies"), ("test_snapshot", "tests")):
            if field in record:
                add(kind, record[field])
    if len(refs) > MAX_REFERENCES:
        raise ValueError("Fitting closure exceeds 512 immutable reference records")
    return [{"kind": k, "id": i, "snapshot": refs[k, i]} for k, i in sorted(refs)]


def _bindings(service, base, training, validation):
    """Restrict application to observed conditions; total targets already include dark."""
    common = training[0]["binding"]
    temperature, gains = common["temperature_c"], common["analog_gains"]
    exposures = [e for record in training for e in record["binding"]["exposure_s"]]
    exposure_range = [min(exposures), max(exposures)]
    artifact = next(a for a in base["artifacts"] if a["name"] == "expected-dark-rate.npy")
    dark_rate = readout_calibrations._dark_rate(
        readout_calibrations._artifact_bytes(service, artifact), base)
    for record in training + validation:
        binding = record["binding"]
        if any(binding[key] != base[key] for key in (
            "sensor_model_id", "physical_instance", "readout_mode", "pixel_pitch_um", "raw_formats"
        )) or binding["geometry"]["full_sensor_shape"] != base["geometry"]["full_sensor_shape"]:
            raise ValueError("Fitting observation differs from the base native readout binding")
        if (binding["temperature_c"] != temperature or binding["analog_gains"] != gains
                or binding["digital_gains"] != common["digital_gains"]):
            raise ValueError(
                "All training and held-out observations require fixed temperature/gains")
        domain = base["applicability"]
        for key, origin, size in zip(
            ("row_range", "column_range"), base["geometry"]["crop_origin"],
            base["dark_rate_shape"], strict=True,
        ):
            low, high = binding["roi"][key]
            if not origin <= low < high <= origin + size:
                raise ValueError("Fitting ROI lies outside the base expected-dark calibration map")
        row0, row1 = binding["roi"]["row_range"]
        col0, col1 = binding["roi"]["column_range"]
        origin = base["geometry"]["crop_origin"]
        selected_dark = dark_rate[row0 - origin[0]:row1 - origin[0],
                                  col0 - origin[1]:col1 - origin[1]]
        factor = readout_calibrations._temperature_factor(base["electrical_model"], temperature)
        for frame, exposure in zip(record["frames"], binding["exposure_s"], strict=True):
            for site, (dr, dc) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
                expected = float(np.max(selected_dark[dr::2, dc::2])) * factor * exposure
                total = frame["mean_total_charge_e"][site]
                # Only relative binary64 product roundoff is tolerated; a positive
                # dark expectation cannot be hidden behind an absolute zero-charge floor.
                tolerance = 8 * np.finfo(float).eps * max(expected, total)
                if expected > total + tolerance:
                    raise ValueError(
                        "Declared total charge is below expected dark; negative photocharge")
        if not domain["temperature_c"][0] <= temperature <= domain["temperature_c"][1]:
            raise ValueError("Fitting temperature is outside the base calibration domain")
        for observed, declared, digital in zip(
            gains, domain["readouts"], binding["digital_gains"], strict=True
        ):
            if not declared["analog_gain"][0] <= observed <= declared["analog_gain"][1] or (
                digital != declared["digital_gain"]
            ):
                raise ValueError("Fitting path gain is outside the base calibration domain")
        for exposure in binding["exposure_s"]:
            if not domain["exposure_s"][0] <= exposure <= domain["exposure_s"][1]:
                raise ValueError("Fitting exposure is outside the base calibration domain")
            if not exposure_range[0] <= exposure <= exposure_range[1]:
                raise ValueError("Held-out exposure is outside the training exposure range")
            from . import nonlinear_dcg, nonlinear_readout

            if base["readout_mode"] == "shared_charge_paths":
                nonlinear_dcg.validate_capture_conditions(base["electrical_model"], exposure,
                                                          temperature)
            else:
                nonlinear_readout.validate_capture_conditions(
                    base["electrical_model"], exposure, gains[0], temperature)
    applicability = deepcopy(base["applicability"])
    applicability.update(temperature_c=[temperature, temperature], exposure_s=exposure_range)
    for domain, gain in zip(applicability["readouts"], gains, strict=True):
        domain["analog_gain"] = [gain, gain]
    model = deepcopy(base["electrical_model"])
    paths = model.get("paths", [model])
    for path, gain in zip(paths, gains, strict=True):
        path["charge_voltage_response"]["valid_domain"].update(
            temperature_c=[temperature, temperature], analog_gain=[gain, gain])
    model["read_noise"]["temperature_range_c"] = [temperature, temperature]
    if len(paths) == 1:
        model["read_noise"]["analog_gain_range"] = [gains[0], gains[0]]
    return model, applicability, None if len(paths) == 2 else gains[0]


def _groups(service, records):
    if sum(r["binding"]["raw_code_samples"] for r in records) > readout_fit_math.MAX_OBSERVATIONS:
        raise ValueError("Combined fitting samples exceed the numerical admission limit")
    pieces = {}
    for record in records:
        for group in readout_observations.groups(service, record)["groups"]:
            pieces.setdefault(group["mean_charge_e"], []).append(group["raw_dn"])
    if len(pieces) > readout_fit_math.MAX_GROUPS:
        raise ValueError("Combined exact charge groups exceed the admission limit")
    return [{"mean_charge_e": mean, "raw_dn": np.concatenate(pieces[mean], axis=0)}
            for mean in sorted(pieces)]


def _prepare(service, payload, *, environment=None):
    from .service import environment_fingerprint

    specification = ReadoutFitRequest.model_validate(payload).model_dump()
    base = readout_calibrations.get(service, specification["base_calibration_id"])
    training = [readout_observations.get(service, i)
                for i in specification["training_observation_ids"]]
    validation = [readout_observations.get(service, i)
                  for i in specification["validation_observation_ids"]]
    if any(r["dataset_role"] != "development" for r in training) or any(
        r["dataset_role"] != "holdout" for r in validation
    ):
        raise ValueError("Fitting requires explicit development and held-out observation roles")
    train_closure, val_closure = _closure(service, training), _closure(service, validation)
    base_lineage = readout_calibrations.lineage(service, base)
    base_used = set(base_lineage["decoder_fitting_tokens"]) | set(
        base_lineage.get("decoder_evaluation_tokens", []))
    if set(val_closure["tokens"]) & (set(train_closure["tokens"]) | base_used):
        raise ValueError("Held-out observations overlap training or base calibration ancestry")
    for records in (training, validation):
        seen = set()
        for record in records:
            own = set(readout_observations.lineage(service, record)["tokens"])
            if seen & own:
                raise ValueError("Observation inputs repeat or relabel overlapping exposures")
            seen.update(own)
    model, applicability, gain = _bindings(service, base, training, validation)
    train_groups, validation_groups = _groups(service, training), _groups(service, validation)
    validation_resources = readout_fit_math.preflight(model, validation_groups, analog_gain=gain)
    # The math owner supplies bounded identifiability/bounds admission without optimization.
    train_resources = readout_fit_math.training_preflight(
        model, train_groups, analog_gain=gain,
        **{k: specification[k] for k in ("slope_scale_bounds", "sigma_v_bounds", "max_iterations")},
    )
    references = _references(base_lineage, train_closure, val_closure)
    evidence = {"base": base_lineage, "training": train_closure, "validation": val_closure}
    artifacts = _unique_artifacts(evidence)
    unique_bytes = {a["sha256"]: a["bytes"] for a in artifacts}
    if sum(unique_bytes.values()) > MAX_INPUT_BYTES:
        raise ValueError("Original fitting evidence exceeds the 64 MiB input budget")
    _verify_artifacts(service, artifacts)
    result = {
        "schema": "camera_readout_fit_plan_v1", "specification": specification,
        "base_calibration_snapshot": base, "training_observations": training,
        "validation_observations": validation, "evidence_closure": evidence,
        "references": references, "training_tokens": train_closure["tokens"],
        "evaluation_tokens": val_closure["tokens"], "fitting_model": model,
        "applicability": applicability, "analog_gain": gain,
        "environment": environment if environment is not None else environment_fingerprint(),
        "input_artifacts": artifacts,
        "resources": {
            "training": train_resources, "validation": validation_resources,
            "input_artifact_bytes": sum(unique_bytes.values()),
            "result_bytes_hard_limit": MAX_RESULT_BYTES,
            "logical_request_bytes_limit": MAX_LOGICAL_BYTES,
            "worker_timeout_s": 120, "reference_records": len(references),
        },
        "physical_validation": "not_established", "authority": deepcopy(AUTHORITY),
    }
    if len(canonical(result).encode()) > MAX_LOGICAL_BYTES:
        raise ValueError("Frozen fitting metadata exceeds the 64 MiB logical request budget")
    from .evidence_package import graph, graph_size

    resources = result["resources"]
    resources.update(forecast_version="camera_readout_fit_graph_forecast_v1",
                     result_graph_bytes_hard_limit=MAX_RESULT_BYTES,
                     result_graph_nodes_hard_limit=MAX_RESULT_GRAPH_NODES,
                     prepared_graph_bytes=0, prepared_graph_nodes=0,
                     uncompressed_evidence_bytes_upper_bound=0)
    for _ in range(8):
        packed = graph(result)
        prepared_bytes, prepared_nodes = graph_size(packed), len(packed["objects"])
        # Original graph is counted exactly. Future result graph and its four JSON
        # artifacts are independently capped at runtime. A graph envelope row is
        # <256 bytes (fixed SHA/path + bounded integer size). 64 nodes/64KiB cover
        # the fixed job, publication-binding and manifest wrappers, not copied data.
        envelope_bound = (prepared_bytes + sum(unique_bytes.values()) + 2 * MAX_RESULT_BYTES
                          + 65536 + 256 * (prepared_nodes + MAX_RESULT_GRAPH_NODES + 64
                                          + len(unique_bytes) + 4) + 4096)
        update = {"prepared_graph_bytes": prepared_bytes, "prepared_graph_nodes": prepared_nodes,
                  "uncompressed_evidence_bytes_upper_bound": envelope_bound}
        if all(resources[k] == v for k, v in update.items()):
            break
        resources.update(update)
    else:
        raise ValueError("Fitting graph forecast failed to stabilize")
    if envelope_bound > 256 * 1024**2:
        raise ValueError("Fitting evidence forecast exceeds the 256 MiB export limit")
    result["plan_hash"] = digest(result)
    return result


def _current(service):
    from .service import environment_fingerprint

    environment = environment_fingerprint()
    if service.loaded_environment != environment:
        raise ConflictError("Executable fitting source changed; restart before admission")
    return environment


def plan(service, payload):
    return _prepare(service, payload, environment=_current(service))


def submit(service, specification, *, plan_hash):
    prepared = plan(service, specification)
    if prepared["plan_hash"] != plan_hash:
        raise ConflictError("Fitting plan changed; inspect a fresh read-only plan")
    request = {**prepared, "job_kind": JOB_KIND, "plugin_id": PLUGIN_ID}
    return service.registry.submit(request, digest(request))


def verify_inputs(service, job, *, base=None):
    """Verify historical evidence without requiring the currently loaded source version."""
    pinned = {k: v for k, v in job.items() if k not in {
        "id", "status", "cache_key", "content_hash", "created_at", "updated_at", "result", "error",
        "artifacts", "cache_hit", "job_kind", "plugin_id",
    }}
    if (job.get("job_kind") != JOB_KIND or job.get("plugin_id") != PLUGIN_ID
            or pinned.get("schema") != "camera_readout_fit_plan_v1"
            or digest({k: v for k, v in pinned.items() if k != "plan_hash"}) != job["plan_hash"]):
        raise IntegrityError("Frozen fitting request or plan differs")
    if digest({**pinned, "job_kind": JOB_KIND, "plugin_id": PLUGIN_ID}) != job["content_hash"]:
        raise IntegrityError("Frozen fitting request hash differs")
    if base is None:
        base = readout_calibrations.get(service, job["specification"]["base_calibration_id"])
    if base != job["base_calibration_snapshot"]:
        raise IntegrityError("Fitting base calibration changed")
    spec = ReadoutFitRequest.model_validate(job["specification"]).model_dump()
    if spec != job["specification"]:
        raise IntegrityError("Frozen fitting request is not normalized")
    for key in ("training_observations", "validation_observations"):
        id_key = "training_observation_ids" if key.startswith("training") else (
            "validation_observation_ids")
        if [r["id"] for r in job[key]] != spec[id_key]:
            raise IntegrityError("Frozen fitting observation IDs differ")
        for record in job[key]:
            if readout_observations.get(service, record["id"]) != record:
                raise IntegrityError("Fitting original observation changed")
    train, validation = job["training_observations"], job["validation_observations"]
    tc, vc = _closure(service, train), _closure(service, validation)
    bc = readout_calibrations.lineage(service, base)
    if job["evidence_closure"] != {"base": bc, "training": tc, "validation": vc} or (
        job["references"] != _references(bc, tc, vc)
    ) or job["training_tokens"] != tc["tokens"] or job["evaluation_tokens"] != vc["tokens"]:
        raise IntegrityError("Frozen fitting evidence closure or exposure tokens differ")
    if (any(r["dataset_role"] != "development" for r in train)
            or any(r["dataset_role"] != "holdout" for r in validation)
            or set(vc["tokens"]) & (set(tc["tokens"]) | set(bc["decoder_fitting_tokens"])
                                    | set(bc.get("decoder_evaluation_tokens", [])))):
        raise IntegrityError("Frozen fitting roles or independence differ")
    for records in (train, validation):
        seen = set()
        for record in records:
            tokens = set(readout_observations.lineage(service, record)["tokens"])
            if seen & tokens:
                raise IntegrityError("Frozen fitting observations repeat exposures")
            seen.update(tokens)
    model, applicability, gain = _bindings(service, base, train, validation)
    if model != job["fitting_model"] or applicability != job["applicability"] or (
        gain != job["analog_gain"]
    ):
        raise IntegrityError("Frozen fitting application domain changed")
    if job["input_artifacts"] != _unique_artifacts(job["evidence_closure"]):
        raise IntegrityError("Frozen fitting artifact closure differs")
    for ref in job["references"]:
        if service.registry.get(ref["kind"], ref["id"]) != ref["snapshot"]:
            raise IntegrityError("Fitting immutable reference closure changed")
    _verify_artifacts(service, job["input_artifacts"])


def _json_artifact(service, name, value):
    content = canonical(value).encode()
    if len(content) > MAX_RESULT_BYTES:
        raise ValueError("Fitting generated artifact exceeds its hard limit")
    return service.registry.store_blob(content, name, "application/json")


def execute(service, job):
    if job["environment"] != _current(service):
        raise ValueError("Worker environment differs from frozen response fitting")
    verify_inputs(service, job)
    spec = job["specification"]
    training = _groups(service, job["training_observations"])
    output = readout_fit_math.fit(
        job["fitting_model"], training, analog_gain=job["analog_gain"],
        **{k: spec[k] for k in ("slope_scale_bounds", "sigma_v_bounds", "max_iterations")},
    )
    candidate = {"electrical_model": output["electrical_model"],
                 "applicability": deepcopy(job["applicability"])}
    candidate["candidate_sha256"] = digest(candidate)
    frozen = canonical(candidate)
    # No held-out codes enter the optimizer. Load/evaluate them only after this freeze.
    evaluation = readout_fit_math.evaluate(
        deepcopy(candidate["electrical_model"]),
        _groups(service, job["validation_observations"]), analog_gain=job["analog_gain"],
    )
    if canonical(candidate) != frozen:
        raise IntegrityError("Held-out evaluation changed the frozen candidate")
    optimizer = deepcopy(output["report"])
    training_report = optimizer.pop("training")
    result = {
        "schema": "camera_readout_fit_result_v1", "candidate": candidate,
        "optimizer_report": optimizer, "training_report": training_report,
        "validation_report": evaluation, "candidate_frozen_before_evaluation": True,
        "training_tokens": job["training_tokens"], "evaluation_tokens": job["evaluation_tokens"],
        "charge_references": [
            deepcopy(r["charge_reference"])
            for r in job["training_observations"] + job["validation_observations"]],
        "physical_validation": "not_established", "authority": deepcopy(AUTHORITY),
        "uncertainty": {"kind": "unknown"}, "approval_status": "unapproved",
    }
    if len(canonical(result).encode()) * 2 > MAX_RESULT_BYTES:
        raise ValueError("Fitting diagnostics exceed the admitted total result budget")
    result["artifacts"] = [
        _json_artifact(service, "fitted-candidate.json", candidate),
        _json_artifact(service, "fit-optimizer.json", optimizer),
        _json_artifact(service, "fit-training.json", training_report),
        _json_artifact(service, "fit-heldout.json", evaluation),
    ]
    from .evidence_package import graph, graph_size

    packed = graph(result)
    if (graph_size(packed) > MAX_RESULT_BYTES or len(packed["objects"]) > MAX_RESULT_GRAPH_NODES
            or sum(a["bytes"] for a in result["artifacts"]) > MAX_RESULT_BYTES):
        raise ValueError("Generated fitting evidence exceeds its admitted graph/artifact budget")
    return result


def verify_result(service, job):
    from . import nonlinear_dcg, nonlinear_readout

    result = job["result"]
    if result.get("schema") != "camera_readout_fit_result_v1":
        raise IntegrityError("Invalid fitted result schema")
    candidate = result["candidate"]
    if digest({k: v for k, v in candidate.items() if k != "candidate_sha256"}) != (
        candidate["candidate_sha256"]
    ) or result["candidate_frozen_before_evaluation"] is not True:
        raise IntegrityError("Fitted candidate identity changed")
    if (candidate["applicability"] != job["applicability"]
            or result["training_tokens"] != job["training_tokens"]
            or result["evaluation_tokens"] != job["evaluation_tokens"]
            or result["authority"] != AUTHORITY
            or result["physical_validation"] != "not_established"):
        raise IntegrityError("Fitted result domain, evidence or authority changed")
    proposed = candidate["electrical_model"]
    paired = "paths" in job["fitting_model"]
    validate = nonlinear_dcg.validate_model if paired else nonlinear_readout.validate_model
    if validate(proposed) != proposed:
        raise IntegrityError("Fitted candidate model is not normalized")
    optimizer = result["optimizer_report"]
    if (optimizer["candidate_model_sha256"] != digest(proposed)
            or optimizer["base_model_sha256"] != digest(job["fitting_model"])):
        raise IntegrityError("Optimizer and fitted model hashes differ")
    expected = deepcopy(job["fitting_model"])
    paths = expected["paths"] if paired else [expected]
    proposed_paths = proposed["paths"] if paired else [proposed]
    if len(optimizer["paths"]) != len(paths):
        raise IntegrityError("Optimizer path count differs")
    if optimizer["converged"] != all(p["converged"] for p in optimizer["paths"]):
        raise IntegrityError("Optimizer convergence summary differs from its path diagnostics")
    sigmas = []
    low, high = job["specification"]["slope_scale_bounds"]
    slo, shi = job["specification"]["sigma_v_bounds"]
    for path, actual, report in zip(paths, proposed_paths, optimizer["paths"], strict=True):
        curve = path["charge_voltage_response"]
        scales, sigma = report["slope_scales"], report["sigma_v"]
        if (len(scales) != len(curve["charge_e"]) - 1
                or any(type(s) not in (int, float) or not low <= s <= high for s in scales)
                or type(sigma) not in (int, float) or not slo <= sigma <= shi):
            raise IntegrityError("Optimizer parameters differ from admitted bounds")
        curve["signal_voltage_v"] = np.r_[
            0.0, np.cumsum(np.diff(curve["signal_voltage_v"]) * scales)
        ].tolist()
        curve["source"] = deepcopy(actual["charge_voltage_response"]["source"])
        sigmas.append(sigma)
    if paired:
        rho = readout_fit_math.inherited_correlation(job["fitting_model"])
        represented = readout_fit_math.fitted_covariance(job["fitting_model"], sigmas)
        expected["read_noise"]["covariance_v2"] = represented["covariance_v2"]
        if optimizer["covariance_representation"] != represented["representation"]:
            raise IntegrityError("Fitted covariance representation differs")
        if (optimizer["inherited_correlation"] != rho
                or optimizer["correlation_fitted"] is not False):
            raise IntegrityError("Fitted paired correlation assumption differs")
    else:
        expected["read_noise"]["variance_v2"] = sigmas[0] ** 2
    expected["read_noise"]["source"] = deepcopy(proposed["read_noise"]["source"])
    expected["source"] = deepcopy(proposed["source"])
    if validate(expected) != proposed:
        raise IntegrityError("Fitted candidate changed frozen electronics or fitted parameters")
    for key, resource_key in (("training_report", "training"), ("validation_report", "validation")):
        resources = result[key]["resources"]
        admitted = job["resources"][resource_key]
        if (resources["model_sha256"] != digest(proposed)
                or resources["raw_input_sha256"] != admitted["raw_input_sha256"]
                or resources["sample_rows"] != admitted["sample_rows"]):
            raise IntegrityError(
                "Fitting/evaluation diagnostics bind different model or RAW samples")
    names = {"fitted-candidate.json": "candidate", "fit-optimizer.json": "optimizer_report",
             "fit-training.json": "training_report", "fit-heldout.json": "validation_report"}
    if (len(result["artifacts"]) != len(names)
            or {a["name"] for a in result["artifacts"]} != set(names)):
        raise IntegrityError("Fitting diagnostics artifact set differs")
    _verify_artifacts(service, result["artifacts"])
    for artifact in result["artifacts"]:
        content = service.registry.blob_path(artifact["sha256"]).read_bytes()
        if content != canonical(result[names[artifact["name"]]]).encode():
            raise IntegrityError("Fitting diagnostics differ from preserved result")


def get_job(service, identity, *, verify=True):
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Identifier does not belong to response fitting")
    if verify:
        verify_inputs(service, job)
    if job["status"] == "succeeded":
        verify_result(service, job)
    return job


def export(service, identity):
    job = get_job(service, identity)
    return {"schema": "camera_readout_fit_evidence_v1", "fit_job": job,
            "publication_binding": {"request_sha256": job["content_hash"],
                                    "result_sha256": digest(job["result"])}
            if job["status"] == "succeeded" else None,
            "physical_validation": "not_established", "authority": AUTHORITY}


def bundle(service, identity):
    from .evidence_package import build_graph_bundle

    manifest = export(service, identity)
    return build_graph_bundle(service, manifest, _unique_artifacts(manifest))
