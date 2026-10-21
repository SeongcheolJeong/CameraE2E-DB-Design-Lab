"""Versioned observations over preserved acquisitions; analyses are not new samples."""

import json
import uuid

from pydantic import Field

from .contracts import Contract, MeasurementCreate
from .control_observation import METHOD, ResponseCaptureImport, analyze, metric_fields
from .imaging import _current
from .observations import load_raw_npy
from .registry import canonical, digest, now

SCHEMA = "camera_capture_analysis_v1"
ACQUISITION_SCHEMA = "camera_response_acquisition_v1"


class CaptureAnalysisRequest(Contract):
    capture_id: str = Field(min_length=1)
    test_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(default="1", min_length=1, max_length=80)
    # A different metric/reduction needs an explicit bound or explicit unknown.
    uncertainty: dict
    parent_analysis_id: str | None = None


def stage(document, identity):
    value = {**document, "id": identity, "created_at": now()}
    return {**value, "content_hash": digest(value)}


def load_acquisition(service, capture):
    """Check original bytes/identity without depending on any derived observation."""
    from .control_response_service import pixel_hash

    if capture.get("capture_kind") != METHOD:
        raise ValueError("This analysis adapter requires a preserved control response acquisition")
    artifacts = {a["name"]: service.registry.blob_path(a["sha256"]) for a in capture["artifacts"]}
    data = ResponseCaptureImport.model_validate_json(
        artifacts["acquisition-metadata.json"].read_bytes()
    ).model_dump()
    if capture.get("capture_schema") == ACQUISITION_SCHEMA:
        if {k: data[k] for k in data if k not in {"test_id", "uncertainty"}} != {
            k: capture[k] for k in data if k not in {"test_id", "uncertainty"}
        } or data["test_id"] != capture["acquisition_test_id"]:
            raise ValueError("Preserved acquisition metadata differs from the capture record")
        origin = service.registry.get("tests", capture["acquisition_test_id"])
        if origin != capture["acquisition_test_snapshot"]:
            raise ValueError("Acquisition Test snapshot differs from its original revision")
    else:
        if data != {k: capture[k] for k in ResponseCaptureImport.model_fields}:
            raise ValueError("Preserved acquisition metadata differs from the legacy capture")
        origin = service.registry.get("tests", capture["test_id"])
    expected_observation = {
        k: origin["observation_specification"][k]
        for k in ("raw_format", "geometry", "frame_shape", "mode", "statistics_policy")
    }
    if capture.get("capture_schema") == ACQUISITION_SCHEMA and (
        capture["observation"] != expected_observation
    ):
        raise ValueError("Preserved RAW format/geometry differs from acquisition evidence")
    raw = load_raw_npy(artifacts["raw-capture.npy"].read_bytes())
    if (
        list(raw.shape) != capture["raw_shape"]
        or pixel_hash(raw) != capture["raw_content_hash"]
        or [pixel_hash(frame) for frame in raw] != capture["raw_frame_hashes"]
    ):
        raise ValueError("Preserved RAW content differs from the acquisition identity")
    return data, origin, raw


def compatible_test(service, origin, test):
    from .control_response_service import validate_registered_test

    validate_registered_test(service, test)
    # Observation/acceptance can change. Execution, hardware, scene, policy,
    # RAW layout and all physical coordinates must retain their original meaning.
    for key in ("stimulus", "conditions", "clock", "frame_conditions", "included_effects"):
        if test[key] != origin[key]:
            raise ValueError("Analysis Test changes the preserved acquisition execution context")
    for key in ("raw_format", "geometry", "frame_shape", "mode", "statistics_policy"):
        if test["observation_specification"][key] != origin["observation_specification"][key]:
            raise ValueError("Analysis Test changes the preserved RAW/statistics context")


def derive_contract(service, capture, test, request):
    """Recompute from the acquisition, never from a previous metric's values/bounds."""
    from .control_response_service import validate_registered_test
    from .validation import _uncertainty

    data, origin, raw = load_acquisition(service, capture)
    compatible_test(service, origin, test)
    validate_registered_test(service, origin)
    # Reuse strict uncertainty/provenance validation; retain original acquisition metadata.
    ResponseCaptureImport.model_validate({**data, "uncertainty": request["uncertainty"]})
    program = service.registry.get("control_programs", test["stimulus"]["control_program_id"])
    if data["assembly_id"] != program["assembly_id"]:
        raise ValueError("Analysis Test changes the preserved camera assembly")
    observation = analyze(raw, test, data["acquisition"])
    contract = MeasurementCreate.model_validate(
        {
            **{
                k: data[k]
                for k in (
                    "source_kind",
                    "source_uri",
                    "license",
                    "method_and_version",
                    "physical_instance",
                    "parents",
                    "assembly_id",
                    "dataset_ids",
                    "instrument",
                    "conditions",
                )
            },
            "name": request["name"],
            "revision": request["revision"],
            "test_id": test["id"],
            "uncertainty": request["uncertainty"],
            **metric_fields(observation),
            "included_effects": test["included_effects"],
            "valid_domain": service._domain_intersection(program["model_snapshots"]),
            "calibration_dataset_ids": [],
            "raw_capture_id": capture["id"],
        }
    ).model_dump()
    _, issues = _uncertainty(
        contract, len(contract["values"]), (len(contract["values"]),), "uncertainty"
    )
    if any(issue["severity"] == "error" for issue in issues):
        raise ValueError("Response metric uncertainty has incompatible units, shape or bounds")
    return contract, observation


def prepare(service, capture, test, request):
    request = CaptureAnalysisRequest.model_validate(request).model_dump()
    if request["capture_id"] != capture["id"] or request["test_id"] != test["id"]:
        raise ValueError("Analysis request differs from its capture or Test revision")
    parent = None
    if request["parent_analysis_id"]:
        parent = service.registry.get("capture_analyses", request["parent_analysis_id"])
        if parent["capture_id"] != capture["id"]:
            raise ValueError("An analysis parent must retain the same acquisition identity")
    contract, observation = derive_contract(service, capture, test, request)
    environment = _current(service)
    request_hash = digest(
        {
            "schema": SCHEMA,
            "request": request,
            "capture_hash": capture["content_hash"],
            "test_hash": test["content_hash"],
            "environment": environment,
            "parent_hash": parent["content_hash"] if parent else None,
        }
    )
    identity = "capture-analysis-" + request_hash
    contract["capture_analysis_id"] = identity
    measurement_id = "measurement-" + uuid.uuid4().hex
    analysis = stage(
        {
            "schema": SCHEMA,
            "request": request,
            "request_hash": request_hash,
            "name": request["name"],
            "revision": request["revision"],
            "capture_id": capture["id"],
            "capture_hash": capture["content_hash"],
            "test_id": test["id"],
            "test_snapshot": test,
            "parent_analysis_id": request["parent_analysis_id"],
            "parent_hash": parent["content_hash"] if parent else None,
            "measurement_id": measurement_id,
            "measurement_contract": contract,
            "analysis_environment": environment,
            "observation": observation,
            "independence": {
                "acquisition_id": capture["id"],
                "dataset_ids": capture["dataset_ids"],
                "raw_content_hash": capture["raw_content_hash"],
                "raw_frame_hashes": capture["raw_frame_hashes"],
                "adds_independent_acquisition": False,
            },
            "artifacts": [
                service.registry.store_blob(
                    canonical(observation).encode(), "response-analysis.json", "application/json"
                )
            ],
            "approval_status": "unreviewed",
            "physical_validation": "not_established",
        },
        identity,
    )
    measurement = stage(
        {
            **contract,
            "original_artifact": service.registry.store_blob(
                canonical(contract).encode(), "measurement.json", "application/json"
            ),
            "original_encoding": "canonical_contract_json",
            "approval_status": "unreviewed",
        },
        measurement_id,
    )
    return analysis, measurement


def create(service, payload):
    request = CaptureAnalysisRequest.model_validate(payload).model_dump()
    capture = service.registry.get("captures", request["capture_id"])
    test = service.registry.get("tests", request["test_id"])
    analysis, measurement = prepare(service, capture, test, request)
    analysis, measurement = service.registry.publish_capture_analysis(
        capture, analysis, measurement
    )
    return {"capture": capture, "analysis": analysis, "measurement": measurement, "test": test}


def verify(service, measurement, capture):
    analysis = service.registry.get("capture_analyses", measurement["capture_analysis_id"])
    test = service.registry.get("tests", analysis["test_id"])
    if (
        analysis["capture_id"] != capture["id"]
        or analysis["capture_hash"] != capture["content_hash"]
        or analysis["test_snapshot"] != test
    ):
        raise ValueError("Analysis differs from its preserved acquisition or Test")
    request = CaptureAnalysisRequest.model_validate(analysis["request"]).model_dump()
    contract, observation = derive_contract(service, capture, test, request)
    contract["capture_analysis_id"] = analysis["id"]
    actual = {k: v for k, v in measurement.items() if k in MeasurementCreate.model_fields}
    if (
        contract != MeasurementCreate.model_validate(actual).model_dump()
        or contract != analysis["measurement_contract"]
        or observation != analysis["observation"]
        or (measurement.get("id") and measurement["id"] != analysis["measurement_id"])
    ):
        raise ValueError("Measurement differs from its preserved acquisition analysis revision")
    if analysis["parent_analysis_id"]:
        parent = service.registry.get("capture_analyses", analysis["parent_analysis_id"])
        if (
            parent["capture_id"] != capture["id"]
            or parent["content_hash"] != analysis["parent_hash"]
        ):
            raise ValueError("Analysis lineage differs from its preserved parent")
    expected_hash = digest(
        {
            "schema": SCHEMA,
            "request": request,
            "capture_hash": capture["content_hash"],
            "test_hash": test["content_hash"],
            "environment": analysis["analysis_environment"],
            "parent_hash": analysis["parent_hash"],
        }
    )
    if (
        analysis["request_hash"] != expected_hash
        or analysis["id"] != "capture-analysis-" + expected_hash
    ):
        raise ValueError("Analysis request differs from its preserved identity")
    for artifact in analysis["artifacts"]:
        if json.loads(service.registry.blob_path(artifact["sha256"]).read_bytes()) != observation:
            raise ValueError("Replayed response differs from its preserved analysis artifact")
    return capture
