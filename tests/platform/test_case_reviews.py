"""Case scope, frozen result integrity and atomic append-only revision boundaries."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from types import SimpleNamespace

import pytest
import test_control_response_workflow as response_workflow

from camera_platform.case_contracts import CaseReviewCreate
from camera_platform.case_reviews import (
    AUTHORITY,
    case_review_history,
    create_case_review,
    get_case_review,
    list_case_reviews,
)
from camera_platform.demo import measurement_template, seed
from camera_platform.registry import (
    ConflictError,
    IntegrityError,
    SQLiteRegistry,
    canonical,
    digest,
)
from camera_platform.service import PlatformService

workflow = response_workflow.workflow


@pytest.fixture
def setup(tmp_path):
    service = SimpleNamespace(registry=SQLiteRegistry(tmp_path))
    registry = service.registry
    assembly = registry.put("assemblies", {"name": "Camera A", "model_ids": []})
    test = registry.put("tests", {"name": "Fixed requirement", "conditions": {"lux": 10}})
    payload = {
        "request_id": "request-initial-001",
        "name": "Low-light concept",
        "question": "Enough SNR?",
        "requirements": [
            {"key": "snr", "description": "SNR under fixed light", "test_id": test["id"]}
        ],
        "assembly_ids": [assembly["id"]],
        "evidence": [],
        "decision": {"status": "open", "rationale": "Need a measured comparison"},
        "unresolved": ["Physical accuracy is unknown"],
        "next_experiments": [
            {
                "question": "How does exposure change SNR?",
                "reason": "Resolve saturation tradeoff",
                "mode": "response_db",
                "conditions": {"exposure_s": {"value": 0.008, "unit": "s"}},
                "assembly_id": assembly["id"],
                "test_id": test["id"],
            }
        ],
    }
    return service, payload


def run_record(service, payload, *, status="succeeded", **overrides):
    request = {
        "assembly_id": payload["assembly_ids"][0],
        "test_id": payload["requirements"][0]["test_id"],
        "parameters": {"seed": 42},
        **overrides,
    }
    registry = service.registry
    job = registry.submit(request, digest(request), force=True)
    if status == "queued":
        return job
    if status == "cancelled":
        return registry.cancel(job["id"])
    assert registry.claim()["id"] == job["id"]
    if status == "running":
        return registry.get_run(job["id"])
    return registry.finish(
        job["id"],
        error="Fixture calculation failed" if status == "failed" else None,
        result=None if status == "failed" else {"summary": {"snr_db": 21}, "artifacts": []},
    )


def attach(payload, kind, identity):
    payload["evidence"] = [{"kind": kind, "id": identity, "requirement_keys": ["snr"]}]


def rows(registry):
    with registry.connection() as connection:
        return {
            table: [dict(row) for row in connection.execute("SELECT * FROM " + table)]
            for table in ("records", "jobs", "audit")
        }


def test_server_freezes_full_results_and_restart_preserves_exact_revision(setup):
    service, payload = setup
    run = run_record(service, payload)
    attach(payload, "runs", run["id"])
    saved = create_case_review(service, payload)
    frozen = saved["snapshots"]["evidence"][0]
    assert frozen["snapshot"] == run
    assert frozen["record_sha256"] == digest(run) != run["content_hash"]
    assert saved["snapshots"]["tests"][0]["id"] == run["test_id"]
    assert saved["authority"] == AUTHORITY
    restarted = SimpleNamespace(registry=SQLiteRegistry(service.registry.directory))
    assert get_case_review(restarted, saved["id"]) == saved
    assert list_case_reviews(restarted) == [saved]
    assert case_review_history(restarted, saved["id"]) == [saved]


def test_revision_is_append_only_and_latest_listing_does_not_rewrite_prior(setup):
    service, payload = setup
    first = create_case_review(service, payload)
    prior_rows = rows(service.registry)
    payload.update(request_id="request-revision-002", previous_revision_id=first["id"])
    payload["decision"] = {
        "status": "candidate",
        "rationale": "Promising model; measurement remains",
    }
    second = create_case_review(service, payload)
    assert second["case_id"] == first["id"] and second["revision"] == 2
    assert get_case_review(service, first["id"]) == first
    assert list_case_reviews(service) == [second]
    assert case_review_history(service, first["id"]) == [first, second]
    after = rows(service.registry)
    assert after["jobs"] == prior_rows["jobs"]
    assert after["records"][:-1] == prior_rows["records"]
    assert after["audit"][:-1] == prior_rows["audit"]
    assert second["authority"] == AUTHORITY
    assert service.registry.audit()["integrity"] == "verified"


def test_latest_listing_decodes_each_historical_graph_once_and_resolves_sources(setup, monkeypatch):
    from camera_platform import case_reviews
    from camera_platform.evidence_storage import PREFIX

    service, payload = setup
    run = run_record(service, payload, authored_values=list(range(70_000)))
    attach(payload, "runs", run["id"])
    first = create_case_review(service, payload)
    latest = create_case_review(service, {
        **payload, "request_id": "large-case-revision", "previous_revision_id": first["id"],
    })
    with service.registry.connection() as connection:
        texts = {row["id"]: row["document"] for row in connection.execute(
            "SELECT id, document FROM records WHERE kind='case_reviews'"
        )}
    assert all(text.startswith(PREFIX) for text in texts.values())
    decoded = Counter()
    original_decode = service.registry.decode_document
    original_resolve = case_reviews._resolve_snapshots
    resolutions = []

    def decode(text):
        decoded[text] += 1
        return original_decode(text)

    def resolve(service, data):
        resolutions.append(data["request_id"])
        return original_resolve(service, data)

    monkeypatch.setattr(service.registry, "decode_document", decode)
    monkeypatch.setattr(case_reviews, "_resolve_snapshots", resolve)
    assert list_case_reviews(service) == [latest]
    assert {identity: decoded[text] for identity, text in texts.items()} == {
        first["id"]: 1, latest["id"]: 1,
    }
    assert resolutions == [latest["request_id"]]


def test_repeated_listing_is_detached_and_rechecks_equal_size_artifact_changes(setup):
    service, payload = setup
    artifact = service.registry.store_blob(b"abcd", "case-input.raw", "application/octet-stream")
    run = run_record(service, payload, input_artifacts=[artifact])
    attach(payload, "runs", run["id"])
    saved = create_case_review(service, payload)
    before = rows(service.registry)
    listed = list_case_reviews(service)
    listed[0]["snapshots"]["evidence"][0]["snapshot"]["result"]["summary"]["snr_db"] = -123
    listed[0]["authority"]["physical_camera_validation"] = "caller mutation"
    assert list_case_reviews(service) == [saved]
    assert get_case_review(service, saved["id"]) == saved

    path = service.registry.blob_path(artifact["sha256"])
    path.write_bytes(b"wxyz")
    unavailable = list_case_reviews(service)[0]
    assert unavailable["schema"] == "camera_case_review_unavailable_v1"
    assert unavailable["id"] == saved["id"]
    with pytest.raises(IntegrityError):
        get_case_review(service, saved["id"])
    assert rows(service.registry) == before
    path.write_bytes(b"abcd")
    assert list_case_reviews(service) == [saved]


@pytest.mark.parametrize("field,value", [
    ("schema", "camera_case_review_unsupported_v9"),
    ("authority", {}),
    ("request_hash", "0" * 64),
])
def test_listing_reused_verified_record_still_checks_case_contract(setup, field, value):
    service, payload = setup
    saved = create_case_review(service, payload)
    changed = {key: value for key, value in saved.items() if key != "content_hash"}
    changed[field] = value
    # Rehash the tampered public row so Registry verification passes. Domain
    # contract validation must still run on the operation-local record.
    with service.registry.transaction() as connection:
        connection.execute("DROP TRIGGER immutable_records_update")
        connection.execute("UPDATE records SET document=?, content_hash=? WHERE id=?", (
            canonical(changed), digest(changed), saved["id"],
        ))
    assert service.registry.get("case_reviews", saved["id"])[field] == value
    with pytest.raises(IntegrityError, match="contract or authority"):
        get_case_review(service, saved["id"])
    listed = list_case_reviews(service)
    assert listed[0]["schema"] == "camera_case_review_unavailable_v1"
    assert "contract or authority" in listed[0]["availability"]["reason"]


def test_listing_still_verifies_corrupt_nonlatest_history(setup):
    service, payload = setup
    first = create_case_review(service, payload)
    latest = create_case_review(service, {
        **payload, "request_id": "healthy-latest", "previous_revision_id": first["id"],
    })
    with service.registry.transaction() as connection:
        connection.execute("DROP TRIGGER immutable_records_update")
        connection.execute("UPDATE records SET document='{}' WHERE id=?", (first["id"],))
    assert get_case_review(service, latest["id"]) == latest
    with pytest.raises(IntegrityError):
        list_case_reviews(service)


def test_request_retry_is_durable_and_changed_request_conflicts(setup):
    service, payload = setup
    saved = create_case_review(service, payload)
    before = rows(service.registry)
    assert create_case_review(service, payload) == saved
    assert rows(service.registry) == before
    payload["question"] = "A different engineering question"
    with pytest.raises(ConflictError, match="different contents"):
        create_case_review(service, payload)
    assert rows(service.registry) == before


def test_stale_previous_conflicts_but_original_retry_stays_idempotent(setup):
    service, payload = setup
    first = create_case_review(service, payload)
    next_payload = {
        **payload,
        "request_id": "revision-one-save",
        "previous_revision_id": first["id"],
    }
    second = create_case_review(service, next_payload)
    before = rows(service.registry)
    with pytest.raises(ConflictError, match="newer revision"):
        create_case_review(service, {**next_payload, "request_id": "revision-other-save"})
    assert create_case_review(service, payload) == first
    assert create_case_review(service, next_payload) == second
    assert rows(service.registry) == before


def test_simultaneous_revisions_publish_only_one_successor(setup, monkeypatch):
    service, payload = setup
    first = create_case_review(service, payload)
    barrier = Barrier(2)
    actual = service.registry.publish_case_review

    def publish(identity, document):
        barrier.wait(timeout=10)
        return actual(identity, document)

    monkeypatch.setattr(service.registry, "publish_case_review", publish)

    def save(number):
        try:
            return create_case_review(
                service,
                {
                    **payload,
                    "request_id": f"parallel-save-{number}",
                    "previous_revision_id": first["id"],
                },
            )
        except ConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, [1, 2]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    assert len(case_review_history(service, first["id"])) == 2


def test_simultaneous_identical_requests_share_one_record_and_audit_event(setup, monkeypatch):
    service, payload = setup
    barrier = Barrier(2)
    actual = service.registry.publish_case_review

    def publish(identity, document):
        barrier.wait(timeout=10)
        return actual(identity, document)

    monkeypatch.setattr(service.registry, "publish_case_review", publish)
    before = rows(service.registry)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: create_case_review(service, payload), range(2)))
    assert results[0] == results[1]
    assert len(rows(service.registry)["audit"]) == len(before["audit"]) + 1


def test_global_pass_and_unrelated_runs_never_change_case_or_its_counts(setup):
    service, payload = setup
    saved = create_case_review(service, payload)
    service.registry.put("validations", {"status": "PASS", "physical_validation_pass": True})
    unrelated = service.registry.put("assemblies", {"name": "Unrelated camera"})
    run_record(service, payload, assembly_id=unrelated["id"])
    other = deepcopy(payload)
    other.update(
        request_id="another-case-request",
        name="Another case",
        assembly_ids=[unrelated["id"]],
        next_experiments=[],
    )
    create_case_review(service, other)
    assert get_case_review(service, saved["id"]) == saved
    assert saved["display_summary"]["evidence"] == 0
    assert saved["authority"]["physical_validation"] is False
    assert len(case_review_history(service, saved["id"])) == 1


@pytest.mark.parametrize("field", ["assembly_id", "test_id"])
def test_unrelated_evidence_identity_is_rejected_without_mutation(setup, field):
    service, payload = setup
    kind = "assemblies" if field == "assembly_id" else "tests"
    unrelated = service.registry.put(kind, {"name": "Unrelated fixed revision"})
    run = run_record(service, payload, **{field: unrelated["id"]})
    attach(payload, "runs", run["id"])
    before = rows(service.registry)
    with pytest.raises(ValueError, match="Evidence (assembly|test)"):
        create_case_review(service, payload)
    assert rows(service.registry) == before


@pytest.mark.parametrize("status", ["queued", "running"])
def test_nonterminal_run_cannot_be_frozen(setup, status):
    service, payload = setup
    run = run_record(service, payload, status=status)
    attach(payload, "runs", run["id"])
    with pytest.raises(ConflictError, match="terminal"):
        create_case_review(service, payload)
    assert service.registry.list("case_reviews") == []


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_and_cancelled_runs_are_saved_as_observations_not_approvals(setup, status):
    service, payload = setup
    run = run_record(service, payload, status=status)
    attach(payload, "runs", run["id"])
    saved = create_case_review(service, payload)
    assert saved["snapshots"]["evidence"][0]["snapshot"]["status"] == status
    assert saved["authority"] == AUTHORITY


@pytest.mark.parametrize(
    "kind", ["characterization", "isp_replay", "surrogate_training", "spectral_scene_render"]
)
def test_non_camera_job_cannot_be_disguised_as_camera_run(setup, kind):
    service, payload = setup
    run = run_record(service, payload, job_kind=kind)
    attach(payload, "runs", run["id"])
    with pytest.raises(ValueError, match="camera simulation"):
        create_case_review(service, payload)


@pytest.mark.parametrize(
    "kind,identity",
    [("measurements", "absent-id"), ("validations", "assembly"), ("runs", "assembly")],
)
def test_unknown_or_wrong_kind_reference_cannot_be_saved(setup, kind, identity):
    service, payload = setup
    attach(payload, kind, payload["assembly_ids"][0] if identity == "assembly" else identity)
    with pytest.raises(KeyError):
        create_case_review(service, payload)
    assert not service.registry.list("case_reviews")


@pytest.mark.parametrize(
    "injection",
    [
        {"authority": {"physical_validation": True}},
        {"snapshots": {}},
        {"revision": 9},
        {"decision": {"status": "approved", "rationale": "Approved"}},
        {"request_id": "../escape"},
        {"decision": {"status": "candidate", "rationale": "   "}},
    ],
)
def test_client_cannot_inject_snapshots_authority_identity_or_empty_decision(setup, injection):
    service, payload = setup
    payload.update(injection)
    with pytest.raises(ValueError):
        create_case_review(service, payload)
    assert not service.registry.list("case_reviews")


@pytest.mark.parametrize("status", ["open", "deferred"])
def test_empty_draft_needs_no_assembly_test_or_evidence(setup, status):
    service, _ = setup
    result = create_case_review(
        service,
        {
            "request_id": "empty-draft-request",
            "name": "New concept",
            "question": "What should we study?",
            "decision": {"status": status, "rationale": "Gather requirements first"},
        },
    )
    assert result["snapshots"] == {"assemblies": [], "tests": [], "evidence": []}
    assert result["display_summary"]["requirements"] == 0


@pytest.mark.parametrize(
    "change",
    ["unknown_requirement", "duplicate_reference", "unfixed_test", "wrong_experiment", "nan"],
)
def test_case_contract_rejects_ambiguous_links_and_nonfinite_conditions(setup, change):
    service, payload = setup
    run = run_record(service, payload)
    attach(payload, "runs", run["id"])
    if change == "unknown_requirement":
        payload["evidence"][0]["requirement_keys"] = ["elsewhere"]
    elif change == "duplicate_reference":
        payload["evidence"] *= 2
    elif change == "unfixed_test":
        payload["requirements"][0]["test_id"] = None
    elif change == "wrong_experiment":
        payload["next_experiments"][0]["assembly_id"] = "not-in-case"
    else:
        payload["next_experiments"][0]["conditions"] = {"exposure": float("nan")}
    with pytest.raises(ValueError):
        CaseReviewCreate.model_validate(payload)


def test_job_result_tampering_is_detected_before_publication(setup):
    service, payload = setup
    run = run_record(service, payload)
    attach(payload, "runs", run["id"])
    with service.registry.transaction() as conn:
        conn.execute("DROP TRIGGER immutable_completed_job")
        conn.execute(
            "UPDATE jobs SET result=? WHERE id=?",
            (canonical({"summary": {"snr_db": 999}}), run["id"]),
        )
    with pytest.raises(IntegrityError, match="result failed audit"):
        create_case_review(service, payload)
    assert not service.registry.list("case_reviews")


def test_frozen_full_result_detects_rehashed_result_even_when_request_hash_unchanged(setup):
    service, payload = setup
    run = run_record(service, payload)
    attach(payload, "runs", run["id"])
    saved = create_case_review(service, payload)
    changed = {"summary": {"snr_db": 999}, "artifacts": []}
    with service.registry.transaction() as conn:
        conn.execute("DROP TRIGGER immutable_completed_job")
        conn.execute("DROP TRIGGER immutable_audit_update")
        conn.execute("UPDATE jobs SET result=? WHERE id=?", (canonical(changed), run["id"]))
        event = dict(
            conn.execute(
                "SELECT * FROM audit WHERE subject=? AND action='runs.succeeded'", (run["id"],)
            ).fetchone()
        )
        event["details"] = {"result_hash": digest(changed), "error": None}
        value = {
            k: event[k] for k in ("timestamp", "action", "subject", "details", "previous_hash")
        }
        conn.execute(
            "UPDATE audit SET details=?,event_hash=? WHERE seq=?",
            (canonical(event["details"]), digest(value), event["seq"]),
        )
    assert service.registry.get_run(run["id"])["content_hash"] == run["content_hash"]
    with pytest.raises(IntegrityError, match="full frozen result"):
        get_case_review(service, saved["id"])


def test_actual_simulation_measurement_validation_links_and_no_authority(tmp_path):
    service = PlatformService(tmp_path)
    seed(service)
    measurement_data = measurement_template(service)
    job = service.submit_run(
        {
            "assembly_id": measurement_data["assembly_id"],
            "test_id": measurement_data["test_id"],
            "parameters": {"frames": 2, "output_size": [8, 8]},
        }
    )
    claimed = service.registry.claim()
    run = service.registry.finish(job["id"], result=service.execute_run(claimed))
    prediction = run["result"]["prediction"]
    for key in ("values", "axes", "conditions", "included_effects"):
        measurement_data[key] = prediction[key]
    measurement = service.create_measurement(measurement_data)
    validation = service.validate(
        {"run_id": run["id"], "measurement_id": measurement["id"], "test_id": run["test_id"]}
    )
    payload = {
        "request_id": "actual-case-evidence",
        "name": "Actual calculation",
        "question": "Compare modeled response",
        "requirements": [
            {"key": "snr", "description": "Fixed response test", "test_id": run["test_id"]}
        ],
        "assembly_ids": [run["assembly_id"]],
        "decision": {"status": "candidate", "rationale": "Synthetic reference only"},
    }
    attach(payload, "validations", validation["id"])
    saved = create_case_review(service, payload)
    linked = saved["snapshots"]["evidence"][0]["linked_records"]
    assert linked[0]["snapshot"]["result"] == run["result"]
    assert linked[0]["record_sha256"] == digest(run)
    assert saved["authority"] == AUTHORITY
    assert get_case_review(service, saved["id"]) == saved
    # A comparison with a different measured assembly is not this fixed design's evidence.
    other = next(a for a in service.registry.list("assemblies") if a["id"] != run["assembly_id"])
    mismatched_measurement = {
        k: v for k, v in measurement.items() if k not in {"id", "created_at", "content_hash"}
    }
    mismatched_measurement["assembly_id"] = other["id"]
    mismatched_measurement = service.registry.put("measurements", mismatched_measurement)
    invalid = service.validate(
        {
            "run_id": run["id"],
            "measurement_id": mismatched_measurement["id"],
            "test_id": run["test_id"],
        }
    )
    payload["request_id"] = "invalid-validation-link"
    payload["assembly_ids"].append(other["id"])
    attach(payload, "validations", invalid["id"])
    with pytest.raises(ValueError, match="one exact assembly"):
        create_case_review(service, payload)


def test_study_requires_all_terminal_candidates_and_freezes_each_result(tmp_path):
    service = PlatformService(tmp_path)
    seed(service)
    demo = service.bootstrap()["demo"]
    spec = {
        "name": "Exposure tradeoff",
        "question": "Exposure sensitivity?",
        "assembly_ids": [demo["assembly_ids"][0]],
        "test_id": demo["test_id"],
        "axes": [{"parameter": "exposure_s", "unit": "s", "values": [0.004, 0.008]}],
        "objectives": [{"metric": "min_theoretical_snr_db", "direction": "maximize"}],
        "frames": 2,
        "output_size": [8, 8],
    }
    plan = service.plan_study(spec)
    study = service.create_study({"specification": spec, "plan_hash": plan["plan_hash"]})
    payload = {
        "request_id": "study-case-evidence",
        "name": "Exposure tradeoff",
        "question": "Enough SNR?",
        "requirements": [
            {"key": "snr", "description": "Fixed baseline test", "test_id": demo["test_id"]}
        ],
        "assembly_ids": spec["assembly_ids"],
        "decision": {"status": "investigate", "rationale": "Compare sampled points"},
    }
    attach(payload, "studies", study["id"])
    with pytest.raises(ConflictError, match="terminal"):
        create_case_review(service, payload)
    while run := service.registry.claim():
        service.registry.finish(run["id"], result=service.execute_run(run))
    saved = create_case_review(service, payload)
    linked = saved["snapshots"]["evidence"][0]["linked_records"]
    assert len(linked) == len(study["run_ids"]) == 2
    assert all(item["snapshot"]["status"] == "succeeded" for item in linked)
    assert all(item["record_sha256"] == digest(item["snapshot"]) for item in linked)
    assert saved["authority"] == AUTHORITY


@pytest.mark.parametrize("source", ["input", "output", "measurement"])
@pytest.mark.parametrize("corruption", ["missing", "modified"])
def test_evidence_blob_corruption_blocks_save_and_restore_without_audit(setup, source, corruption):
    service, payload = setup
    artifact = service.registry.store_blob(
        b"original retained evidence", "retained.raw", "application/octet-stream"
    )
    if source == "measurement":
        record = service.registry.put(
            "measurements",
            {
                "assembly_id": payload["assembly_ids"][0],
                "test_id": payload["requirements"][0]["test_id"],
                "original_artifact": artifact,
                "values": [12.0],
            },
        )
        attach(payload, "measurements", record["id"])
    else:
        record = run_record(
            service, payload, **({"input_artifacts": [artifact]} if source == "input" else {})
        )
        if source == "output":
            # A separate normal job whose actual finish result includes the output blob.
            record = service.registry.submit(
                {"assembly_id": record["assembly_id"], "test_id": record["test_id"]},
                "output-blob",
                force=True,
            )
            service.registry.claim()
            record = service.registry.finish(record["id"], result={"artifacts": [artifact]})
        attach(payload, "runs", record["id"])
    saved = create_case_review(service, payload)
    path = service.registry.blob_path(artifact["sha256"])
    path.unlink() if corruption == "missing" else path.write_bytes(b"corrupted")
    before = rows(service.registry)
    with pytest.raises((IntegrityError, KeyError)):
        create_case_review(service, {**payload, "request_id": "blob-failure-save"})
    with pytest.raises((IntegrityError, KeyError)):
        get_case_review(service, saved["id"])
    assert rows(service.registry) == before


@pytest.mark.parametrize("corruption", ["missing", "modified"])
def test_unavailable_latest_case_does_not_hide_healthy_cases_or_rewrite_history(setup, corruption):
    service, payload = setup
    healthy = create_case_review(service, payload)
    first = create_case_review(service, {**payload, "request_id": "damaged-case-first"})
    artifact = service.registry.store_blob(
        b"retained case input", "case-input.raw", "application/octet-stream"
    )
    run = run_record(service, payload, input_artifacts=[artifact])
    revised = {**payload, "request_id": "damaged-case-revised", "previous_revision_id": first["id"]}
    attach(revised, "runs", run["id"])
    latest = create_case_review(service, revised)
    path = service.registry.blob_path(artifact["sha256"])
    original = path.read_bytes()
    path.unlink() if corruption == "missing" else path.write_bytes(b"wrong bytes")
    before = rows(service.registry)

    listed = {record["case_id"]: record for record in list_case_reviews(service)}
    assert listed[healthy["case_id"]] == healthy
    unavailable = listed[first["case_id"]]
    assert unavailable["schema"] == "camera_case_review_unavailable_v1"
    assert unavailable["availability"]["status"] == "unavailable"
    assert unavailable["availability"]["reason"]
    assert unavailable["authority"] == AUTHORITY
    identity_fields = {
        "id",
        "case_id",
        "name",
        "question",
        "revision",
        "previous_revision_id",
        "created_at",
    }
    assert set(unavailable) == identity_fields | {"schema", "availability", "authority"}
    assert {key: unavailable[key] for key in identity_fields} == {
        key: latest[key] for key in identity_fields
    }
    assert get_case_review(service, healthy["id"]) == healthy
    assert get_case_review(service, first["id"]) == first
    with pytest.raises((IntegrityError, KeyError)):
        get_case_review(service, latest["id"])
    with pytest.raises((IntegrityError, KeyError)):
        case_review_history(service, latest["id"])
    assert rows(service.registry) == before

    # A repaired original blob restores the exact immutable review, not a
    # replacement revision or a new audit event.
    path.write_bytes(original)
    restored = {record["case_id"]: record for record in list_case_reviews(service)}
    assert restored[first["case_id"]] == latest
    assert rows(service.registry) == before


def test_case_listing_does_not_downgrade_registry_metadata_corruption(setup):
    service, payload = setup
    saved = create_case_review(service, payload)
    with service.registry.transaction() as connection:
        connection.execute("DROP TRIGGER immutable_records_update")
        connection.execute("UPDATE records SET document='{}' WHERE id=?", (saved["id"],))
    with pytest.raises(IntegrityError):
        list_case_reviews(service)


def test_raw_measurement_freezes_capture_analysis_and_rejects_missing_acquisition(workflow):
    from camera_platform import control_response_service as response

    service, test, run = response_workflow.response_run(workflow)
    metadata, raw = response_workflow.source(service, test, run)
    imported = response.import_capture(service, metadata, raw)
    measurement = imported["measurement"]
    payload = {
        "request_id": "raw-acquisition-case",
        "name": "Retained RAW response",
        "question": "What did the camera do?",
        "assembly_ids": [run["assembly_id"]],
        "requirements": [
            {"key": "snr", "description": "Fixed observation test", "test_id": test["id"]}
        ],
        "decision": {"status": "investigate", "rationale": "Synthetic RAW observation only"},
    }
    attach(payload, "measurements", measurement["id"])
    saved = create_case_review(service, payload)
    linked = saved["snapshots"]["evidence"][0]["linked_records"]
    assert {(item["kind"], item["id"]) for item in linked} == {
        ("captures", measurement["raw_capture_id"]),
        ("capture_analyses", measurement["capture_analysis_id"]),
    }
    raw_artifact = next(
        item for item in imported["capture"]["artifacts"] if item["name"] == "raw-capture.npy"
    )
    service.registry.blob_path(raw_artifact["sha256"]).unlink()
    before = rows(service.registry)
    with pytest.raises(KeyError):
        create_case_review(service, {**payload, "request_id": "missing-raw-new-save"})
    with pytest.raises(KeyError):
        get_case_review(service, saved["id"])
    assert rows(service.registry) == before


def test_declared_calibrated_validation_requires_both_assemblies_and_registered_lineage(setup):
    from camera_platform.calibration_service import calibrated_assembly_equivalence

    service, payload = setup
    registry = service.registry
    lens = registry.put("models", {"kind": "lens"})
    isp = registry.put("models", {"kind": "isp"})
    sensor = registry.put("models", {"kind": "sensor", "asset_id": "same-sensor", "parents": []})
    calibrated = registry.put(
        "models", {"kind": "sensor", "asset_id": "same-sensor", "parents": [sensor["id"]]}
    )
    contract = {"raw_format": {}, "conditions": {}, "spectral_range": {}}
    original = registry.put(
        "assemblies", {**contract, "model_ids": [lens["id"], sensor["id"], isp["id"]]}
    )
    candidate = registry.put(
        "assemblies", {**contract, "model_ids": [lens["id"], calibrated["id"], isp["id"]]}
    )
    calibration = registry.put(
        "calibrations", {"candidate_model_id": calibrated["id"], "status": "PROPOSED"}
    )
    payload.update(assembly_ids=[candidate["id"], original["id"]], next_experiments=[])
    run = run_record(service, payload)
    artifact = registry.store_blob(
        b"preserved comparison input", "measurement.json", "application/json"
    )
    measurement = registry.put(
        "measurements",
        {"test_id": run["test_id"], "assembly_id": original["id"], "original_artifact": artifact},
    )
    test = registry.get("tests", run["test_id"])
    validation_data = {
        "run_id": run["id"],
        "measurement_id": measurement["id"],
        "test_id": test["id"],
        "allow_calibrated_model": True,
        "status": "INCONCLUSIVE",
        "evidence_hashes": {
            "run": run["content_hash"],
            "measurement": measurement["content_hash"],
            "test": test["content_hash"],
        },
        "model_revision_comparison": calibrated_assembly_equivalence(
            service, candidate["id"], original["id"]
        ),
    }
    validation = registry.put("validations", validation_data)
    attach(payload, "validations", validation["id"])
    saved = create_case_review(service, payload)
    assert any(
        item["id"] == calibration["id"]
        for item in saved["snapshots"]["evidence"][0]["linked_records"]
    )
    assert saved["authority"] == AUTHORITY
    with pytest.raises(ValueError, match="assembly"):
        create_case_review(
            service,
            {**payload, "request_id": "missing-measured-design", "assembly_ids": [candidate["id"]]},
        )
    validation = registry.put("validations", {**validation_data, "model_revision_comparison": {}})
    attach(payload, "validations", validation["id"])
    with pytest.raises(IntegrityError, match="calibration lineage"):
        create_case_review(service, {**payload, "request_id": "wrong-calibration-link"})


def test_run_with_overridden_effective_test_is_not_evidence_for_original_conditions(setup):
    service, payload = setup
    original = service.registry.get("tests", payload["requirements"][0]["test_id"])
    run = run_record(service, payload, test_snapshot={**original, "conditions": {"lux": 1000}})
    attach(payload, "runs", run["id"])
    with pytest.raises(ValueError, match="conditions differ"):
        create_case_review(service, payload)


def test_case_insert_and_audit_rollback_together(setup, monkeypatch):
    service, payload = setup
    before = rows(service.registry)

    def fail_audit(*args):
        raise RuntimeError("Audit unavailable")

    monkeypatch.setattr(service.registry, "_audit", fail_audit)
    with pytest.raises(RuntimeError, match="Audit unavailable"):
        create_case_review(service, payload)
    assert rows(service.registry) == before
