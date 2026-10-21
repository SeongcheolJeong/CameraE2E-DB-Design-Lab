"""Durable registered-source wrapper for bounded retained-code joint ISP studies."""

from __future__ import annotations

import io
import itertools
import math
import tempfile
import zipfile
from copy import deepcopy
from pathlib import Path

import numpy as np

from . import retained_isp_joint_study as engine
from .imaging import _current
from .registry import ConflictError, IntegrityError, canonical, digest
from .retained_isp_comparison import _artifacts, _verify_artifacts
from .retained_isp_source import load_inputs, resolve, validate_selector
from .temporal_signal_reference import load_truth, validate_registered_test
from .validation import check_domain

SCHEMA = "camera_retained_isp_joint_workflow_v1"
JOB_KIND = "retained_isp_joint_study"
PLUGIN_ID = "retained_isp_joint_study_v1"
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_SUMMARY_BYTES = 2 * 1024 * 1024
MAX_FROZEN_BYTES = 32 * 1024 * 1024
NPY_HEADER_BYTES_LIMIT = 512


def _exact(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(label + " requires exactly its declared fields")


def _identity(value, label):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 200:
        raise ValueError(label + " requires a bounded nonblank identifier")
    return value


def _source_request(value):
    _exact(value, ("source_id", "selector"), "Joint Study source")
    return {"source_id": _identity(value["source_id"], "source_id"),
            "selector": validate_selector(value["selector"])}


def validate_specification(payload):
    required = {"schema", "name", "isp_model_id", "stationary_sources",
                "dynamic_source", "temporal_test_id", "axes", "noise_roi",
                "objectives", "constraints"}
    if not isinstance(payload, dict) or not required <= payload.keys() or (
            payload.keys() - required - {"force"}):
        raise ValueError("Joint Study request requires exactly its declared fields")
    if payload["schema"] != SCHEMA or type(payload.get("force", False)) is not bool:
        raise ValueError("Joint Study schema or force is invalid")
    repeats = payload["stationary_sources"]
    if (not isinstance(repeats, list)
            or not engine.MIN_REPEATS <= len(repeats) <= engine.MAX_REPEATS):
        raise ValueError("Joint Study needs four to sixteen independent run sources")
    result = {key: deepcopy(payload[key]) for key in required}
    for key in ("name", "isp_model_id", "temporal_test_id"):
        result[key] = _identity(result[key], key)
    result["stationary_sources"] = [_source_request(item) for item in repeats]
    result["dynamic_source"] = _source_request(payload["dynamic_source"])
    identifiers = [item["source_id"] for item in result["stationary_sources"]]
    identifiers.append(result["dynamic_source"]["source_id"])
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Joint Study acquisitions require distinct source IDs")
    return result


def _prepared(service, payload, *, historical_environment=None, budget_version=3):
    from .retained_isp import validate_profile

    request = validate_specification(payload)
    model = service.registry.get("models", request["isp_model_id"])
    if model.get("kind") != "isp":
        raise ValueError("Joint Study requires a registered ISP model")
    validate_profile(model["payload"])

    def source_pair(item):
        source = resolve(service, item["source_id"], item["selector"])
        return source, load_inputs(service, source)

    repeats = [source_pair(item) for item in request["stationary_sources"]]
    dynamic = source_pair(request["dynamic_source"])
    for source, _ in [*repeats, dynamic]:
        for frame in source["frame_conditions"]:
            conditions = {
                **source["conditions"],
                "exposure_s": {"value": frame["exposure_ns"] * 1e-9, "unit": "s"},
                "gain": {"value": frame["gain"], "unit": "1"},
            }
            if check_domain(conditions, model["valid_domain"]):
                raise ValueError("Joint Study ISP applicability excludes an original frame")
    test = service.registry.get("tests", request["temporal_test_id"])
    reference = validate_registered_test(service, test)
    binding = reference["source_binding"]
    source = dynamic[0]
    if (binding["source_id"] != source["source_id"]
            or binding["selector"] != source["selector"]
            or binding["isp_model_id"] != model["id"]
            or binding["isp_model_sha256"] != digest(model)
            or binding["raw_sha256"] != source["raw_sha256"]
            or binding["selected_array_sha256"] != source["selected_array_sha256"]
            or binding["source_snapshot_sha256"] != digest(source["source_snapshot"])):
        raise ValueError("Joint Study temporal Test does not bind the exact dynamic RAW/ISP")
    for item in request["constraints"]:
        if (isinstance(item, dict) and item.get("metric") == "signal_change_rms_error"
                and isinstance(item.get("value"), (float, int))
                and not isinstance(item["value"], bool)
                and item["value"] > test["absolute_tolerance"]["value"]):
            raise ValueError("Joint Study signal-error limit exceeds the frozen Test tolerance")
    specification = engine.make_specification(
        model, repeats, dynamic, reference,
        axes=request["axes"], noise_roi=request["noise_roi"],
        objectives=request["objectives"], constraints=request["constraints"],
    )
    # This public loader re-resolves the original RAW, truth and registered
    # Test lineage; no caller-supplied truth or processor arrays are accepted.
    load_truth(service, reference)
    truth_artifact = reference["array_artifact"]
    truth_bytes = service.registry.blob_path(truth_artifact["sha256"]).read_bytes()
    # The engine's exact binding check includes the fixed color profile,
    # original Test, selected RAW, row clock and authenticated truth bytes.
    engine._temporal_truth(dynamic[0], dynamic[1], model,
                           validate_profile(model["payload"]), reference, truth_bytes)
    exports = [service.export_run(item["source_id"])
               for item in [*request["stationary_sources"], request["dynamic_source"]]]
    sources = [source for source, _ in [*repeats, dynamic]]
    artifacts = _artifacts([exports, sources, model, test, reference])
    _verify_artifacts(service, artifacts)
    current_environment = _current(service)
    frozen = {
        "job_kind": JOB_KIND, "plugin_id": PLUGIN_ID, "name": request["name"],
        "specification": request,
        "engine_specification": specification,
        "isp_model_snapshot": deepcopy(model),
        "stationary_source_snapshots": deepcopy(sources[:-1]),
        "dynamic_source_snapshot": deepcopy(sources[-1]),
        "source_exports": exports,
        "temporal_test_snapshot": deepcopy(test),
        "temporal_reference_snapshot": deepcopy(reference),
        "input_artifacts": artifacts,
        "environment": deepcopy(current_environment if historical_environment is None
                                else historical_environment),
    }
    frozen_bytes = len(canonical(frozen).encode())
    if frozen_bytes > MAX_FROZEN_BYTES:
        raise ValueError("Joint Study frozen provenance exceeds its metadata budget")
    candidate_count = math.prod(len(axis["values"]) for axis in specification["axes"])
    source_bytes = sum(artifact["bytes"] for artifact in artifacts)
    source_count = len(sources)
    selected_pixels = sum(raw.size for _, raw in [*repeats, dynamic])
    # Every retained invocation preserves float64 pre-tone RGB and boolean gate;
    # v3 also preserves the delivered float64 display RGB for cold verification.
    # A NPY header is bounded separately for each array, before job submission.
    if budget_version not in (1, 2, 3):
        raise IntegrityError("Joint Study budget version is unsupported")
    arrays_per_source = {1: 0, 2: 2, 3: 3}[budget_version]
    output_artifact_count = candidate_count * source_count * arrays_per_source
    output_array_bytes_bound = candidate_count * (
        selected_pixels * ((6 if budget_version == 3 else 3)
                           * np.dtype("float64").itemsize + np.dtype("bool").itemsize)
        + source_count * arrays_per_source * NPY_HEADER_BYTES_LIMIT
    ) if budget_version > 1 else 0
    # ZIP stores aliases separately. The manifest contains the full frozen job,
    # result and artifact descriptors, with a deliberately generous overhead.
    manifest_bound = 4 * frozen_bytes + 4 * MAX_SUMMARY_BYTES + 1024 * 1024
    archive_bound = (source_bytes + output_array_bytes_bound + MAX_SUMMARY_BYTES
                     + manifest_bound
                     + (len(artifacts) + output_artifact_count + 3) * 4096 + 65536)
    if archive_bound > MAX_ARCHIVE_BYTES:
        raise ValueError("Joint Study complete input/output bundle exceeds archive budget")
    cost = {
        "schema": f"camera_retained_isp_joint_budget_v{budget_version}",
        "candidate_count": candidate_count,
        "source_count": source_count,
        "processor_invocations": candidate_count * source_count,
        "total_selected_raw_pixels": selected_pixels,
        "selected_raw_pixel_limit": engine.MAX_TOTAL_RAW_PIXELS,
        "input_artifact_bytes": source_bytes,
        "summary_bytes_limit": MAX_SUMMARY_BYTES,
        "frozen_metadata_bytes": frozen_bytes,
        "manifest_bytes_bound": manifest_bound,
        "archive_bytes_bound": archive_bound,
        "archive_bytes_limit": MAX_ARCHIVE_BYTES,
    }
    if budget_version > 1:
        cost["output_artifact_count"] = output_artifact_count
        cost["output_array_bytes_bound"] = output_array_bytes_bound
    frozen["cost"] = cost
    return frozen, (model, repeats, dynamic, reference, truth_bytes)


def plan(service, payload):
    frozen, _ = _prepared(service, payload)
    return {"plan_hash": digest(frozen), "frozen_request": frozen,
            "cost": deepcopy(frozen["cost"])}


def submit(service, payload, plan_hash):
    proposal = plan(service, payload)
    if not isinstance(plan_hash, str) or plan_hash != proposal["plan_hash"]:
        raise ConflictError("Joint Study source/Test/profile/specification changed after planning")
    document = {**proposal["frozen_request"], "plan_hash": plan_hash}
    job = service.registry.submit(document, digest(document), force=payload.get("force", False))
    return verify(service, job)


def _load_saved_array(service, artifact, shape, dtype):
    if not isinstance(artifact, dict) or not {"sha256", "bytes", "name"} <= artifact.keys():
        raise IntegrityError("Joint Study saved output artifact is malformed")
    if type(artifact["bytes"]) is not int:
        raise IntegrityError("Joint Study saved output byte length is malformed")
    expected_bytes = math.prod(shape) * dtype.itemsize
    if (artifact["bytes"] < expected_bytes
            or artifact["bytes"] > expected_bytes + NPY_HEADER_BYTES_LIMIT
            or artifact.get("media_type") != "application/x-npy"):
        raise IntegrityError("Joint Study saved output exceeds its NPY shape/byte contract")
    content = service.registry.blob_path(artifact["sha256"]).read_bytes()
    if len(content) != artifact["bytes"]:
        raise IntegrityError("Joint Study saved output byte length differs")
    if not content.startswith(b"\x93NUMPY"):
        raise IntegrityError("Joint Study saved output is not a NPY array")
    stream = io.BytesIO(content)
    try:
        value = np.load(stream, allow_pickle=False, max_header_size=NPY_HEADER_BYTES_LIMIT)
    except (EOFError, OSError, ValueError) as error:
        raise IntegrityError("Joint Study saved output is not a bounded NPY array") from error
    if (not isinstance(value, np.ndarray) or stream.tell() != len(content)
            or value.shape != shape or value.dtype != dtype
            or not value.flags.c_contiguous or not np.isfinite(value).all()):
        raise IntegrityError("Joint Study saved output has different native values/type")
    return value


def _verified(service, job, *, current_environment=False):
    if job.get("job_kind") != JOB_KIND or job.get("plugin_id") != PLUGIN_ID:
        raise ValueError("Job is not a retained ISP Joint Study")
    cost = job.get("cost")
    budget_schema = cost.get("schema") if isinstance(cost, dict) else None
    budget_version = {
        "camera_retained_isp_joint_budget_v1": 1,
        "camera_retained_isp_joint_budget_v2": 2,
        "camera_retained_isp_joint_budget_v3": 3,
    }.get(budget_schema)
    if budget_version is None:
        raise IntegrityError("Joint Study frozen budget schema is unsupported")
    fresh, inputs = _prepared(
        service, job["specification"],
        historical_environment=None if current_environment else job["environment"],
        budget_version=budget_version,
    )
    for key, value in fresh.items():
        if canonical(job.get(key)) != canonical(value):
            raise IntegrityError("Frozen Joint Study differs from retained source: " + key)
    if job.get("plan_hash") != digest(fresh):
        raise IntegrityError("Joint Study plan hash differs from immutable request")
    result = job.get("result")
    if result is not None:
        if result.get("physical_validation") != "not_established":
            raise IntegrityError("Joint Study result wrapper changed physical validation")
        artifacts = _artifacts(result["artifacts"])
        _verify_artifacts(service, artifacts)
        summary = result["summary"]
        expected = {
            "job_kind": JOB_KIND, "plugin_id": PLUGIN_ID,
            "plan_hash": job["plan_hash"],
            "specification": job["engine_specification"],
            "specification_sha256": digest(job["engine_specification"]),
            "temporal_test_id": job["temporal_test_snapshot"]["id"],
            "temporal_reference_id": job["temporal_reference_snapshot"]["id"],
            "cost": job["cost"],
            "physical_validation": "not_established",
            "uncertainty": {"kind": "unknown"},
            "noise_definition": engine.NOISE_DEFINITION,
            "noise_meaning": (engine.NOISE_MEANING if budget_version == 3
                              else engine.LEGACY_NOISE_MEANING),
            "automatic_selection": False,
            "truth_role": "evaluation_only_never_processor_input",
        }
        if any(canonical(summary.get(key)) != canonical(value)
               for key, value in expected.items()):
            raise IntegrityError("Joint Study result lost frozen source, plan or authority")
        expected_schema = {
            1: "camera_retained_isp_joint_study_result_v1",
            2: engine.RESULT_SCHEMA_V2,
            3: engine.RESULT_SCHEMA,
        }[budget_version]
        if summary.get("schema") != expected_schema:
            raise IntegrityError("Joint Study result/evidence schema differs from frozen budget")
        if (summary.get("ranking", {}).get("selected_candidate_id") is not None
                or len(summary.get("cases", [])) != job["cost"]["candidate_count"]):
            raise IntegrityError("Joint Study ranking/candidate count differs")
        from .retained_isp import validate_profile

        base = validate_profile(job["isp_model_snapshot"]["payload"])
        axes = job["engine_specification"]["axes"]
        for index, (values, case) in enumerate(zip(
            itertools.product(*(axis["values"] for axis in axes)),
            summary["cases"], strict=True,
        )):
            candidate = deepcopy(base)
            coordinates = {}
            for axis, value in zip(axes, values, strict=True):
                group, key = axis["parameter"].split(".")
                candidate[group][key] = value
                coordinates[axis["parameter"]] = value
            if (case.get("candidate_id") != f"candidate-{index:03d}"
                    or case.get("coordinates") != coordinates
                    or case.get("profile_sha256") != digest(validate_profile(candidate))):
                raise IntegrityError("Joint Study candidate differs from frozen profile grid")
            metrics = case.get("metrics")
            if not isinstance(metrics, dict) or set(metrics) != set(engine.UNITS):
                raise IntegrityError("Joint Study candidate metric set differs")
            for name, metric in metrics.items():
                if not isinstance(metric, dict) or set(metric) != {
                        "value", "unit", "status", "reason"}:
                    raise IntegrityError("Joint Study metric record is malformed")
                value, status = metric["value"], metric["status"]
                if metric["unit"] != engine.UNITS[name] or status not in {"ok", "unknown"}:
                    raise IntegrityError("Joint Study metric unit/status differs")
                if status == "ok":
                    if (type(value) not in (float, int) or not math.isfinite(value)
                            or metric["reason"] is not None):
                        raise IntegrityError("Joint Study measured metric is not finite")
                elif value is not None or not isinstance(metric["reason"], str):
                    raise IntegrityError("Joint Study unknown metric has a value")
            if (case.get("noise_support_status") != metrics[engine.NOISE]["status"]
                    or case.get("noise_support_reason") != metrics[engine.NOISE]["reason"]
                    or case.get("temporal_support_status") not in {"ok", "unknown"}
                    or len(case.get("repeat_processing", []))
                    != len(job["stationary_source_snapshots"])):
                raise IntegrityError("Joint Study support or repeat evidence differs")
            for evidence, source in zip(case["repeat_processing"],
                                        job["stationary_source_snapshots"], strict=True):
                if (evidence.get("source_id") != source["source_id"]
                        or evidence.get("selected_array_sha256")
                        != source["selected_array_sha256"]
                        or evidence.get("processor_profile_sha256")
                        != case["profile_sha256"]):
                    raise IntegrityError("Joint Study repeat processing lineage differs")
            dynamic_evidence = case.get("dynamic_processing", {})
            dynamic_source = job["dynamic_source_snapshot"]
            if (dynamic_evidence.get("source_id") != dynamic_source["source_id"]
                    or dynamic_evidence.get("selected_array_sha256")
                    != dynamic_source["selected_array_sha256"]
                    or dynamic_evidence.get("processor_profile_sha256")
                    != case["profile_sha256"]):
                raise IntegrityError("Joint Study dynamic processing lineage differs")
        if canonical(summary.get("ranking")) != canonical(engine._rank(
                summary["cases"], job["engine_specification"])):
            raise IntegrityError("Joint Study ranking differs from its saved metrics")
        if budget_version > 1:
            output_artifacts = [artifact for artifact in artifacts
                                if artifact["name"] != "summary.json"]
            referenced = _artifacts(summary["cases"])
            if (canonical(output_artifacts) != canonical(referenced)
                    or len(output_artifacts) != job["cost"]["output_artifact_count"]
                    or sum(item["bytes"] for item in output_artifacts)
                    > job["cost"]["output_array_bytes_bound"]):
                raise IntegrityError("Joint Study retained output artifact closure differs")
            engine.verify_saved_evidence(
                job["engine_specification"], *inputs, summary,
                lambda artifact, shape, dtype: _load_saved_array(
                    service, artifact, shape, dtype,
                ),
            )
        saved = [artifact for artifact in artifacts if artifact["name"] == "summary.json"]
        if (len(saved) != 1 or saved[0]["bytes"] > job["cost"]["summary_bytes_limit"]
                or service.registry.blob_path(saved[0]["sha256"]).read_bytes()
                != canonical(summary).encode()):
            raise IntegrityError("Joint Study summary differs from immutable bytes")
    return job, inputs


def verify(service, job, current_environment=False):
    return _verified(service, job, current_environment=current_environment)[0]


def execute(service, job):
    model, repeats, dynamic, reference, truth_bytes = _verified(
        service, job, current_environment=True,
    )[1]
    budget_version = int(job["cost"]["schema"].rsplit("v", 1)[-1])
    legacy = budget_version == 1
    output_artifacts = []
    output_bytes = 0

    def retain_array(name, value):
        nonlocal output_bytes
        stream = io.BytesIO()
        np.save(stream, np.ascontiguousarray(value), allow_pickle=False)
        content = stream.getvalue()
        if (len(content) > value.nbytes + NPY_HEADER_BYTES_LIMIT
                or output_bytes + len(content) > job["cost"]["output_array_bytes_bound"]):
            raise IntegrityError("Joint Study output array exceeded prequeue byte budget")
        artifact = service.registry.store_blob(content, name, "application/x-npy")
        output_artifacts.append(artifact)
        output_bytes += len(content)
        return artifact

    evaluated = engine.evaluate(
        job["engine_specification"], model, repeats, dynamic, reference, truth_bytes,
        retain_array=None if legacy else retain_array, output_version=budget_version,
    )
    if not legacy and len(output_artifacts) != job["cost"]["output_artifact_count"]:
        raise IntegrityError("Joint Study omitted retained candidate output arrays")
    summary = {
        **evaluated,
        "job_kind": JOB_KIND, "plugin_id": PLUGIN_ID,
        "plan_hash": job["plan_hash"],
        "temporal_test_id": job["temporal_test_snapshot"]["id"],
        "temporal_reference_id": reference["id"],
        "cost": deepcopy(job["cost"]),
    }
    content = canonical(summary).encode()
    if len(content) > job["cost"]["summary_bytes_limit"]:
        raise IntegrityError("Joint Study summary exceeded its prequeue budget")
    artifact = service.registry.store_blob(content, "summary.json", "application/json")
    return {"summary": summary, "artifacts": [artifact, *output_artifacts],
            "physical_validation": "not_established"}


def list_jobs(service):
    return [verify(service, job) for job in service.registry.list_runs()
            if job.get("job_kind") == JOB_KIND]


def get(service, identity):
    return verify(service, service.registry.get_run(identity))


def cancel(service, identity):
    get(service, identity)
    return service.registry.cancel(identity)


def export_manifest(service, identity):
    job = get(service, identity)
    artifacts = _artifacts([job["input_artifacts"], job.get("artifacts", []), job.get("result")])
    _verify_artifacts(service, artifacts)
    manifest = {
        "schema": f"camera_retained_isp_joint_export_v{job['cost']['schema'].rsplit('v', 1)[-1]}",
        "study": job,
        "artifacts": artifacts,
        "replay": {
            "specification": job["specification"], "plan_hash": job["plan_hash"],
            "instruction": (
                "Replan against every original retained run, registered ISP model "
                "and temporal Test. For retained-output results, recompute saved metrics "
                "from candidate pre-tone/gate arrays, original RAW and registered truth "
                "without rerunning ISP; v3 also checks final display RGB against the "
                "frozen tone transfer."
                if job["cost"]["schema"] != "camera_retained_isp_joint_budget_v1"
                else "Replan against every original retained run, registered ISP model "
                "and temporal Test."
            ),
        },
        "physical_validation": "not_established", "automatic_selection": False,
    }
    if len(canonical(manifest).encode()) > job["cost"]["manifest_bytes_bound"]:
        raise IntegrityError("Joint Study manifest exceeded its prequeue budget")
    return manifest


def export_bundle(service, identity):
    manifest = export_manifest(service, identity)
    directory = service.registry.directory / "downloads"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="retained-joint-", suffix=".zip", dir=directory, delete=False,
    ) as stream:
        path = Path(stream.name)
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("manifest.json", canonical(manifest).encode())
            archive.writestr(
                "README.txt",
                "Authored code-domain study; truth independence is declared, physical validation "
                "and automatic candidate selection are not established.\n"
                + ("Retained candidate pre-tone RGB and temporal-gate NPY arrays support "
                   "numeric metric verification with original RAW and registered truth.\n"
                   if manifest["schema"] != "camera_retained_isp_joint_export_v1" else "")
                + ("V3 final display RGB NPY arrays are checked against the frozen "
                   "pre-tone output and tone transfer.\n"
                   if manifest["schema"] == "camera_retained_isp_joint_export_v3" else ""),
            )
            for artifact in manifest["artifacts"]:
                archive.write(service.registry.blob_path(artifact["sha256"]),
                              f"artifacts/{artifact['sha256']}/{artifact['name']}")
        if path.stat().st_size > manifest["study"]["cost"]["archive_bytes_bound"]:
            raise IntegrityError("Joint Study ZIP exceeded its prequeue budget")
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise
