"""Saved design reasoning, scoped evidence and immutable revision history.

An engineering note never confers physical validation or release authority.
Result-bearing full snapshots are hashed separately from job request hashes.
"""

from __future__ import annotations

from copy import deepcopy

from .case_contracts import CaseReviewCreate
from .registry import ConflictError, IntegrityError, digest

AUTHORITY = {
    "engineering_note": True,
    "physical_validation": False,
    "release_approval": False,
}
TERMINAL = {"succeeded", "failed", "cancelled"}


def _identity(request_id):
    return "case-review-" + digest(request_id)


def _entry(kind, record):
    return {
        "kind": kind,
        "id": record["id"],
        "record_sha256": digest(record),
        "snapshot": deepcopy(record),
    }


def _verify_artifacts(service, record):
    for artifact in [*record.get("artifacts", []), *record.get("input_artifacts", [])]:
        service.registry.blob_path(artifact["sha256"])


def _measurement_links(service, measurement):
    service.registry.blob_path(measurement["original_artifact"]["sha256"])
    linked = []
    if measurement.get("raw_capture_id"):
        from .observations import verify_capture_measurement

        capture = verify_capture_measurement(service, measurement)
        _verify_artifacts(service, capture)
        linked.append(_entry("captures", capture))
        analysis_id, seen = measurement.get("capture_analysis_id"), set()
        while analysis_id:
            if analysis_id in seen:
                raise IntegrityError("Capture analysis lineage contains a cycle")
            seen.add(analysis_id)
            analysis = service.registry.get("capture_analyses", analysis_id)
            if analysis["capture_id"] != capture["id"]:
                raise IntegrityError("Capture analysis belongs to another acquisition")
            _verify_artifacts(service, analysis)
            linked.append(_entry("capture_analyses", analysis))
            analysis_id = analysis.get("parent_analysis_id")
    return linked


def _run(service, identity):
    from .service import NON_CAMERA_JOBS

    record = service.registry.get_run(identity)
    if record.get("job_kind") in NON_CAMERA_JOBS:
        raise ValueError("Case run evidence requires a camera simulation run")
    if record["status"] not in TERMINAL:
        raise ConflictError("Case evidence must be terminal before it can be frozen")
    if not record.get("assembly_id") or not record.get("test_id"):
        raise ValueError("Case run evidence requires assembly and test identities")
    if record.get("test_snapshot") is not None and record["test_snapshot"] != service.registry.get(
        "tests", record["test_id"]
    ):
        raise ValueError("Run conditions differ from its fixed test revision; register a new test")
    _verify_artifacts(service, record)
    return record


def _match(record, assembly_ids, test_ids):
    if record.get("assembly_id") not in assembly_ids:
        raise ValueError("Evidence assembly is outside this case's fixed designs")
    if test_ids != {record.get("test_id")}:
        raise ValueError("Evidence test differs from its linked case requirements")


def _study(service, record, assembly_ids, test_ids):
    spec = record["specification"]
    if not spec["assembly_ids"] or not set(spec["assembly_ids"]) <= assembly_ids:
        raise ValueError("Study base assemblies are outside this case")
    if test_ids != {spec["test_id"]}:
        raise ValueError("Study base test differs from its linked case requirements")
    candidates, run_ids = record["candidates"], record["run_ids"]
    if not candidates or len(candidates) != len(run_ids) or len(set(run_ids)) != len(run_ids):
        raise IntegrityError("Study candidate/run linkage is incomplete")
    linked = []
    for candidate, run_id in zip(candidates, run_ids, strict=True):
        run = _run(service, run_id)
        if (
            candidate["base_assembly_id"] not in spec["assembly_ids"]
            or run["assembly_id"] != candidate["assembly_id"]
            or run["test_id"] != candidate["test_id"]
        ):
            raise IntegrityError("Study run differs from its frozen candidate")
        linked.append(_entry("runs", run))
    # Plans alone contain no results. Freeze every terminal candidate result too.
    return linked


def _isp_study(service, record, assembly_ids, requirements, reference, reader):
    """Bind each dataset to its own acquisition test and freeze full terminal evidence."""
    manifest = reader.export(record["id"])
    verified = manifest["study"]
    if verified != record:
        raise IntegrityError("ISP study differs from its registered manifest")
    datasets = {item["key"]: item for item in record["datasets"]}
    if not datasets or len(datasets) != len(record["datasets"]):
        raise IntegrityError("ISP study has missing or duplicate datasets")
    if any(item["assembly_id"] not in assembly_ids for item in datasets.values()):
        raise ValueError("Every ISP source acquisition assembly must be fixed in the case")
    for dataset in datasets.values():
        source = dataset["source"]
        test = service.registry.get("tests", dataset["test_id"])
        if source["source_kind"] == "run":
            # Standalone studies may explore an overridden acquisition, but a
            # case requirement still names one exact registered test revision.
            acquisition = _run(service, source["source_id"])
        elif source["source_kind"] == "capture":
            acquisition = service.registry.get("captures", source["source_id"])
            _verify_artifacts(service, acquisition)
            if acquisition.get("capture_kind") == "control_response_observation_v1":
                from .capture_analysis import compatible_test, load_acquisition

                _, origin, _ = load_acquisition(service, acquisition)
                compatible_test(service, origin, test)
            else:
                from .observations import verify_capture_measurement

                verify_capture_measurement(service, {
                    **acquisition["measurement_contract"],
                    "raw_capture_id": acquisition["id"],
                })
                specification = deepcopy(acquisition["observation"])
                specification.pop("source_kind", None)
                if (
                    specification.get("conditions") != test["conditions"]
                    or acquisition["measurement_contract"]["test_id"] != test["id"]
                    or test.get("observation_specification") != specification
                    or test.get("observation_method") != acquisition["observation_method"]
                    or test.get("observation_shape") != acquisition["raw_shape"][-2:]
                ):
                    raise ValueError("ISP capture observation differs from its fixed case Test")
        else:
            raise ValueError("ISP case evidence requires an original run or capture")
        if acquisition != source["source_snapshot"]:
            raise IntegrityError("ISP source acquisition differs from its exact frozen snapshot")
        _match(acquisition, assembly_ids, {dataset["test_id"]})
        if source["conditions"] != test["conditions"]:
            raise ValueError("ISP acquisition conditions differ from its fixed case Test")
    for key, requirement_keys in reference["dataset_requirements"].items():
        if key not in datasets:
            raise ValueError("Case ISP scope names a dataset outside this study")
        test_id = datasets[key]["test_id"]
        if not test_id or {requirements[k]["test_id"] for k in requirement_keys} != {test_id}:
            raise ValueError("ISP dataset acquisition test differs from its case requirements")
    analysis = manifest["analysis"]
    if analysis["status"] != "complete":
        raise ConflictError("All ISP study cells must be terminal before freezing case evidence")
    linked = [_entry("isp_study_exports", {"id": record["id"], "manifest": manifest})]
    if reference.get("selection_id"):
        selection = reader.selection(reference["selection_id"])
        if (
            selection["study_id"] != record["id"]
            and record["specification"].get("selection_id") != selection["id"]
        ):
            raise ValueError("Selected ISP candidate is not linked to this study")
        if selection["calibration_id"] != record["specification"]["calibration_id"]:
            raise IntegrityError("ISP selection calibration differs from the study")
        linked.append(_entry("isp_selections", selection))
    return linked


def _resolve_snapshots(service, data):
    from .isp_studies import _EvidenceReader

    # This reader belongs only to this evidence resolution. It cannot outlive
    # the public read or hide a changed source/completion on a later request.
    isp_reader = _EvidenceReader(service)
    registry = service.registry
    assembly_ids = set(data["assembly_ids"])
    requirements = {item["key"]: item for item in data["requirements"]}
    test_ids = sorted({item["test_id"] for item in requirements.values() if item["test_id"]})
    result = {
        "assemblies": [registry.get("assemblies", identity) for identity in data["assembly_ids"]],
        "tests": [registry.get("tests", identity) for identity in test_ids],
        "evidence": [],
    }
    for ref in data["evidence"]:
        kind, identity = ref["kind"], ref["id"]
        tests = {requirements[key]["test_id"] for key in ref["requirement_keys"]}
        record = _run(service, identity) if kind == "runs" else registry.get(kind, identity)
        linked = []
        if kind in {"runs", "measurements"}:
            _match(record, assembly_ids, tests)
            if kind == "measurements":
                linked = _measurement_links(service, record)
        elif kind == "validations":
            run = _run(service, record["run_id"])
            measurement = registry.get("measurements", record["measurement_id"])
            test = registry.get("tests", record["test_id"])
            _match(run, assembly_ids, tests)
            _match(measurement, assembly_ids, tests)
            if run["status"] != "succeeded" or record["test_id"] != run["test_id"]:
                raise ValueError("Validation must bind one exact assembly and test revision")
            if measurement["assembly_id"] != run["assembly_id"]:
                if not record.get("allow_calibrated_model"):
                    raise ValueError("Validation must bind one exact assembly and test revision")
                from .calibration_service import calibrated_assembly_equivalence

                equivalence = calibrated_assembly_equivalence(
                    service, run["assembly_id"], measurement["assembly_id"]
                )
                if equivalence != record.get("model_revision_comparison"):
                    raise IntegrityError("Validation calibration lineage differs from its sources")
                linked.append(
                    _entry(
                        "calibrations", registry.get("calibrations", equivalence["calibration_id"])
                    )
                )
            if record.get("evidence_hashes") != {
                "run": run["content_hash"],
                "measurement": measurement["content_hash"],
                "test": test["content_hash"],
            }:
                raise IntegrityError("Validation source hashes differ from their linked records")
            linked = [
                _entry("runs", run),
                _entry("measurements", measurement),
                _entry("tests", test),
                *linked,
                *_measurement_links(service, measurement),
            ]
        elif kind == "studies":
            linked = _study(service, record, assembly_ids, tests)
        elif kind == "isp_studies":
            linked = _isp_study(service, record, assembly_ids, requirements, ref, isp_reader)
        result["evidence"].append(
            {
                **_entry(kind, record),
                "requirement_keys": list(ref["requirement_keys"]),
                "linked_records": linked,
                **({
                    "dataset_requirements": deepcopy(ref["dataset_requirements"]),
                    "selection_id": ref["selection_id"],
                } if kind == "isp_studies" else {}),
            }
        )
    return result


def _summary(data):
    return {
        "requirements": len(data["requirements"]),
        "assemblies": len(data["assembly_ids"]),
        "evidence": len(data["evidence"]),
        "evidence_kind_counts": {
            kind: sum(ref["kind"] == kind for ref in data["evidence"])
            for kind in (
                "runs", "measurements", "validations", "studies",
                *(
                    ["isp_studies"]
                    if any(e["kind"] == "isp_studies" for e in data["evidence"]) else []
                ),
            )
        },
        "unresolved": len(data["unresolved"]),
        "next_experiments": len(data["next_experiments"]),
    }


def create_case_review(service, payload):
    data = CaseReviewCreate.model_validate(payload).model_dump()
    identity, request_hash = _identity(data["request_id"]), digest(data)
    try:
        previous_attempt = service.registry.get("case_reviews", identity)
    except KeyError:
        previous_attempt = None
    if previous_attempt is not None:
        if previous_attempt["request_hash"] != request_hash:
            raise ConflictError("Case request_id was already used with different contents")
        return get_case_review(service, identity)
    previous_id = data["previous_revision_id"]
    previous = get_case_review(service, previous_id) if previous_id else None
    document = {
        **data,
        "schema": "camera_case_review_v1",
        "request_hash": request_hash,
        "case_id": previous["case_id"] if previous else identity,
        "revision": previous["revision"] + 1 if previous else 1,
        "snapshots": _resolve_snapshots(service, data),
        "authority": deepcopy(AUTHORITY),
        "display_summary": _summary(data),
    }
    return service.registry.publish_case_review(identity, document)


def get_case_review(service, identity):
    record = service.registry.get("case_reviews", identity)
    return _verify_case_review(service, record)


def _verify_case_review(service, record):
    """Validate one operation's Registry-verified record against its live evidence."""
    data = CaseReviewCreate.model_validate(
        {key: record[key] for key in CaseReviewCreate.model_fields}
    ).model_dump()
    if (
        record.get("schema") != "camera_case_review_v1"
        or record["id"] != _identity(data["request_id"])
        or record["request_hash"] != digest(data)
        or record["authority"] != AUTHORITY
        or record["display_summary"] != _summary(data)
    ):
        raise IntegrityError("Case review contract or authority differs from its frozen request")
    # Public records/jobs are immutable; re-resolving detects result or source tampering.
    if record["snapshots"] != _resolve_snapshots(service, data):
        raise IntegrityError("Case evidence differs from its full frozen result snapshots")
    return record


def list_case_reviews(service):
    """List latest revisions, isolating unavailable evidence from healthy cases."""
    latest = {}
    # Registry metadata verification remains strict. Only a selected case's
    # evidence failure may become an unavailable summary, never an unchecked row.
    for record in service.registry.list("case_reviews"):
        case_id = record["case_id"]
        if case_id not in latest or record["revision"] > latest[case_id]["revision"]:
            latest[case_id] = record
    result = []
    for record in sorted(
        latest.values(), key=lambda item: (item["created_at"], item["id"]), reverse=True
    ):
        try:
            result.append(_verify_case_review(service, record))
        except (IntegrityError, KeyError, ValueError) as error:
            result.append(
                {
                    "schema": "camera_case_review_unavailable_v1",
                    **{
                        key: record[key]
                        for key in (
                            "id",
                            "case_id",
                            "name",
                            "question",
                            "revision",
                            "previous_revision_id",
                            "created_at",
                        )
                    },
                    "availability": {"status": "unavailable", "reason": str(error)},
                    "authority": deepcopy(AUTHORITY),
                }
            )
    return result


def case_review_history(service, identity):
    selected = get_case_review(service, identity)
    records = sorted(
        (r for r in service.registry.list("case_reviews") if r["case_id"] == selected["case_id"]),
        key=lambda item: item["revision"],
    )
    previous_id = None
    for revision, record in enumerate(records, start=1):
        if record["revision"] != revision or record["previous_revision_id"] != previous_id:
            raise IntegrityError("Case history has a missing revision or an unexpected fork")
        previous_id = record["id"]
    return [get_case_review(service, record["id"]) for record in records]
