"""Frozen response Tests, actual Worker observations and preserved acquired RAW."""

import hashlib
import json
import uuid

from . import control_service
from .contracts import MeasurementCreate
from .control_observation import (
    METHOD,
    ResponseCaptureImport,
    ResponseSpecification,
    ResponseTestRequest,
    analyze,
    method,
    metric,
    metric_fields,
    validate_test,
)
from .imaging import _current
from .observations import _validate_sensor_geometry, load_raw_npy
from .registry import ConflictError, canonical


def test_template(service, payload):
    options = ResponseTestRequest.model_validate(payload).model_dump()
    program = service.registry.get("control_programs", options.pop("control_program_id"))
    test = control_service.test_template(service, program["id"])
    image = program["scene_sequence_snapshot"]["image_snapshots"][0]
    raw = program["assembly_snapshot"]["raw_format"]
    spec = ResponseSpecification.model_validate(
        {
            **options,
            "raw_format": {
                "cfa": raw["cfa"],
                "bit_depth": raw["bit_depth"],
                "black_level_dn": raw["black_level"],
                "packing": "unpacked",
                "layout": "bayer",
                "response_type": "linear",
                "hdr_streams": 1,
            },
            "geometry": {
                k: image["sampling"][k] for k in ("kind", "full_sensor_shape", "crop_origin")
            },
            "frame_shape": image["shape"][:2],
            "mode": program["payload"]["mode"],
            "statistics_policy": program["control_policy_snapshot"]["payload"],
        }
    ).model_dump()
    metric_id, unit = metric(spec)
    test.update(
        name="폐루프 응답 관측 · " + options["observable"] + " · " + options["reduction"],
        revision="response-1",
        metric_id=metric_id,
        unit=unit,
        observation_method=method(spec),
        observation_specification=spec,
        observation_shape=spec["frame_shape"],
        absolute_tolerance={"value": 0.01 if unit == "1" else 1.0, "unit": unit},
    )
    validate_test(test)
    return test


def validate_registered_test(service, test):
    spec = validate_test(test).model_dump()
    options = {k: v for k, v in spec.items() if k in ResponseTestRequest.model_fields}
    expected = test_template(
        service,
        {
            **options,
            "control_program_id": test["stimulus"]["control_program_id"],
        },
    )
    for key in (
        "metric_id",
        "unit",
        "reference_plane",
        "clock",
        "frame_conditions",
        "conditions",
        "required_conditions",
        "stimulus",
        "observation_specification",
        "observation_shape",
        "observation_method",
    ):
        if test.get(key) != expected[key]:
            raise ValueError(
                "Response Test must pin the actual program, geometry, policy and observation"
            )
    if set(test["included_effects"]) != set(expected["included_effects"]):
        raise ValueError("Response Test changes the implemented observation effects")


def acquisition_template(service, test_id):
    """An explicitly incomplete import form, never invented instrument evidence."""
    test = service.registry.get("tests", test_id)
    validate_registered_test(service, test)
    stimulus = test["stimulus"]
    program = service.registry.get("control_programs", stimulus["control_program_id"])
    return {
        "name": "RAW 응답 취득",
        "revision": "1",
        "source_kind": "estimated",
        "source_uri": "",
        "license": "",
        "method_and_version": "",
        "physical_instance": None,
        "parents": [],
        "test_id": test_id,
        "assembly_id": program["assembly_id"],
        "dataset_ids": [],
        "instrument": {},
        "conditions": test["conditions"],
        "acquisition": {
            "clock": test["clock"],
            "test_clock_offset_ns": 0,
            "alignment_absolute_bound_ns": None,
            "alignment_source_uri": "",
            "alignment_method_and_version": "",
            "program_hash": stimulus["control_program_hash"],
            "scene_hash": stimulus["scene_sequence_hash"],
            "policy_hash": stimulus["control_policy_hash"],
            "execution_source_uri": "",
            "execution_method_and_version": "",
            "frames": [
                {
                    "frame_id": f["frame_id"],
                    "raw_index": None,
                    "frame_start_ns": None,
                    "latch_at_ns": None,
                    "applied": None,
                }
                for f in stimulus["planned_frames"]
            ],
            "commands": [],
        },
        "uncertainty": {
            "kind": "unknown",
            "unit": test["unit"],
            "reason": "이 지표의 총 불확실도는 아직 확립되지 않았습니다.",
        },
    }


def acquisition_from_run(job, summary):
    test, program = job["test_snapshot"], job["control_program_snapshot"]
    trace = summary["control_trace"]
    return {
        "clock": program["payload"]["clock"],
        "test_clock_offset_ns": 0,
        "alignment_absolute_bound_ns": 0,
        "alignment_source_uri": "run://" + job["id"],
        "alignment_method_and_version": "integer_declared_simulation_clock:1",
        "program_hash": test["stimulus"]["control_program_hash"],
        "scene_hash": test["stimulus"]["scene_sequence_hash"],
        "policy_hash": test["stimulus"]["control_policy_hash"],
        "execution_source_uri": "run://" + job["id"],
        "execution_method_and_version": "closed_loop_scene_v1",
        "frames": [
            {
                "frame_id": f["frame_id"],
                "raw_index": i,
                "frame_start_ns": f["frame_start_ns"],
                "latch_at_ns": f["latch_at_ns"],
                "applied": f["applied"],
            }
            for i, f in enumerate(trace["frames"])
        ],
        "commands": [
            {
                "requested": c["proposal"].get("requested"),
                "commanded": c["proposal"].get("commanded"),
                **{
                    k: c.get(k)
                    for k in (
                        "command_id",
                        "kind",
                        "source_frame_id",
                        "status",
                        "issued_at_ns",
                        "applied_at_ns",
                        "applied_frame_id",
                    )
                },
            }
            for c in trace["commands"]
        ],
    }


def observe_run(job, output):
    summary = output["summary"]
    observation = analyze(
        output["arrays"]["raw_frames"], job["test_snapshot"], acquisition_from_run(job, summary)
    )
    summary["response_observation"] = observation
    summary["prediction"].update(
        **metric_fields(observation),
        uncertainty={
            "kind": "unknown",
            "unit": observation["unit"],
            "reason": "Total model, numerical, RAW sampling and response uncertainty "
            "has not been established for this response operator.",
        },
        comparison_support=METHOD,
        method_and_version="closed_loop_scene_v1+" + METHOD,
    )


def pixel_hash(raw):
    result = hashlib.sha256(canonical(list(raw.shape)).encode())
    result.update(raw.astype("<u4", copy=False).tobytes(order="C"))
    return result.hexdigest()


def _check_independence(service, data, frame_hashes):
    for existing in service.registry.list("measurements"):
        if set(existing["dataset_ids"]) & set(data["dataset_ids"]):
            raise ConflictError("Dataset identity is already registered")
    for capture in service.registry.list("captures"):
        if set(capture.get("raw_frame_hashes", [])) & set(frame_hashes):
            raise ConflictError(
                "RAW frames overlap an existing capture; preserve its original identity"
            )
    if data["source_kind"] != "measured":
        return
    # Renaming an existing simulation export is not independent hardware evidence.
    # Hash normalized pixels so dtype/NPY encoding changes cannot bypass this check.
    seen = set()
    for run in service.registry.list_runs():
        if run["status"] != "succeeded":
            continue
        for artifact in run.get("result", {}).get("artifacts", []):
            if artifact["name"] != "raw_frames.npy" or artifact["sha256"] in seen:
                continue
            seen.add(artifact["sha256"])
            generated = load_raw_npy(service.registry.blob_path(artifact["sha256"]).read_bytes())
            if any(pixel_hash(frame) in frame_hashes for frame in generated):
                raise ConflictError(
                    "Measured RAW duplicates generated pixels; simulation is not physical evidence"
                )


def import_capture(service, metadata, raw_bytes, *, original_text=None):
    data = ResponseCaptureImport.model_validate(metadata).model_dump()
    test = service.registry.get("tests", data["test_id"])
    validate_registered_test(service, test)
    program = service.registry.get("control_programs", test["stimulus"]["control_program_id"])
    if data["assembly_id"] != program["assembly_id"] or data["conditions"] != test["conditions"]:
        raise ValueError("Acquisition camera and static conditions must match the frozen Test")
    assembly = program["assembly_snapshot"]
    _validate_sensor_geometry(
        assembly, program["model_snapshots"], test["observation_specification"], data["source_kind"]
    )
    for frame in data["acquisition"]["frames"]:
        if (
            frame["applied"]
            and frame["applied"]["isp_model_id"] != program["payload"]["initial"]["isp_model_id"]
        ):
            raise ValueError("Acquisition uses a different fixed ISP; create an explicit new Test")
    for parent in data["parents"]:
        service.registry.get("captures", parent)
    raw = load_raw_npy(raw_bytes)
    analyze(raw, test, data["acquisition"])
    frame_hashes = [pixel_hash(f) for f in raw]
    _check_independence(service, data, frame_hashes)
    original = original_text.encode() if original_text is not None else canonical(metadata).encode()
    if len(original) > 1_048_576:
        raise ValueError("Response acquisition metadata exceeds 1 MiB")
    if ResponseCaptureImport.model_validate(json.loads(original)).model_dump() != data:
        raise ValueError("Original metadata differs from its normalized capture contract")
    artifacts = [
        service.registry.store_blob(raw_bytes, "raw-capture.npy", "application/octet-stream"),
        service.registry.store_blob(original, "acquisition-metadata.json", "application/json"),
    ]
    from .capture_analysis import ACQUISITION_SCHEMA, prepare, stage

    capture = stage(
        {
            **{k: v for k, v in data.items() if k not in {"test_id", "uncertainty"}},
            "capture_kind": METHOD,
            "capture_schema": ACQUISITION_SCHEMA,
            "acquisition_test_id": test["id"],
            "acquisition_test_snapshot": test,
            "observation": {
                k: test["observation_specification"][k]
                for k in ("raw_format", "geometry", "frame_shape", "mode", "statistics_policy")
            },
            "raw_frame_hashes": frame_hashes,
            "raw_content_hash": pixel_hash(raw),
            "raw_shape": list(raw.shape),
            "artifacts": artifacts,
            "approval_status": "unreviewed",
            "physical_validation": "not_established",
            "registration_environment": _current(service),
        },
        "capture-" + uuid.uuid4().hex,
    )
    analysis, measurement = prepare(
        service,
        capture,
        test,
        {
            "capture_id": capture["id"],
            "test_id": test["id"],
            "name": data["name"],
            "revision": data["revision"],
            "uncertainty": data["uncertainty"],
        },
    )
    analysis, measurement = service.registry.publish_capture_analysis(
        capture, analysis, measurement, new_capture=True
    )
    return {"capture": capture, "analysis": analysis, "measurement": measurement, "test": test}


def verify_capture(service, measurement, capture):
    if measurement.get("capture_analysis_id"):
        from .capture_analysis import verify

        return verify(service, measurement, capture)
    if capture.get("capture_schema"):
        raise ValueError("Response measurements require their preserved analysis revision")
    expected = {**capture["measurement_contract"], "raw_capture_id": capture["id"]}
    actual = {k: v for k, v in measurement.items() if k in MeasurementCreate.model_fields}
    if (
        MeasurementCreate.model_validate(actual).model_dump()
        != MeasurementCreate.model_validate(expected).model_dump()
    ):
        raise ValueError("Measurement differs from its preserved response RAW analysis")
    artifacts = {a["name"]: service.registry.blob_path(a["sha256"]) for a in capture["artifacts"]}
    data = ResponseCaptureImport.model_validate_json(
        artifacts["acquisition-metadata.json"].read_bytes()
    ).model_dump()
    if data != {k: capture[k] for k in ResponseCaptureImport.model_fields}:
        raise ValueError("Preserved acquisition metadata differs from the capture record")
    test = service.registry.get("tests", capture["test_id"])
    raw = load_raw_npy(artifacts["raw-capture.npy"].read_bytes())
    analysis = analyze(raw, test, data["acquisition"])
    if (
        analysis != capture["analysis_measurement"]
        or json.loads(artifacts["response-analysis.json"].read_bytes()) != analysis
        or [pixel_hash(f) for f in raw] != capture["raw_frame_hashes"]
    ):
        raise ValueError("Replayed response observation differs from the preserved evidence")
    return capture
