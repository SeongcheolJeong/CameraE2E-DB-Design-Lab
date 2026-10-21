"""Evidence storage must detect changed inputs/results and preserve provenance."""

import hashlib
import json
import sqlite3
from copy import deepcopy

import pytest

from camera_platform.demo import measurement_template, seed
from camera_platform.registry import (
    ConflictError,
    IntegrityError,
    SQLiteRegistry,
    canonical,
    digest,
)
from camera_platform.service import PlatformService


@pytest.fixture
def registry(tmp_path):
    return SQLiteRegistry(tmp_path / "registry")


@pytest.fixture
def service(tmp_path):
    result = PlatformService(tmp_path / "platform")
    seed(result)
    return result


def finish_fixture_run(registry, *, document=None, result=None):
    frozen = document or {"request": {"seed": 42, "exposure_s": 0.004}}
    queued = registry.submit(frozen, digest(frozen))
    claimed = registry.claim()
    assert claimed["id"] == queued["id"]
    return registry.finish(
        claimed["id"], result=result or {"prediction": {"values": [1.0]}, "artifacts": []}
    )


def measurement_payload(service, dataset_id="measurement:camera-serial-42"):
    payload = measurement_template(service)
    payload.update(
        name="Independent measured camera sample",
        source_kind="measured",
        source_uri="lab://instrument-7/raw-chart-session-42",
        physical_instance="camera-serial-42",
        method_and_version="raw-chart-analysis:1",
        dataset_ids=[dataset_id],
        instrument={"id": "instrument-7", "calibration_revision": "calibration-2026-09"},
        uncertainty={"kind": "absolute_bound", "value": 0.01, "unit": "1"},
    )
    return payload


def test_record_revision_identity_cannot_be_replaced(registry):
    original = registry.put("models", {"name": "original", "values": [1, 2]}, identity="model:1")
    with pytest.raises(ConflictError):
        registry.put("models", {"name": "replacement", "values": [9, 9]}, identity="model:1")
    assert registry.get("models", "model:1") == original


@pytest.mark.parametrize("operation", ["UPDATE records SET document='{}'", "DELETE FROM records"])
def test_database_triggers_protect_frozen_records(registry, operation):
    registry.put("models", {"name": "frozen"})
    with registry.connection() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(operation)


def test_returned_dictionaries_cannot_mutate_stored_revisions(registry):
    original = registry.put("models", {"response": {"values": [1, 2]}})
    original["response"]["values"][0] = 999
    read = registry.get("models", original["id"])
    assert read["response"]["values"] == [1, 2]
    read["response"]["values"].append(3)
    assert registry.get("models", original["id"])["response"]["values"] == [1, 2]


def test_tampered_record_payload_is_detected_after_storage_bypass(registry):
    original = registry.put("models", {"response": [1, 2]})
    with registry.connection() as connection:
        connection.execute("DROP TRIGGER immutable_records_update")
        connection.execute("UPDATE records SET document=? WHERE id=?", ("{}", original["id"]))
    with pytest.raises(IntegrityError, match="integrity"):
        registry.get("models", original["id"])


def test_audit_links_every_event_and_failed_insert_adds_no_event(registry):
    original = registry.put("assets", {"name": "A"}, identity="asset:1")
    registry.put("models", {"asset_id": original["id"]})
    before = registry.audit()
    with pytest.raises(ConflictError):
        registry.put("assets", {"name": "duplicate"}, identity="asset:1")
    after = registry.audit()
    assert after == before
    assert len(after["events"]) == 2
    assert after["events"][0]["previous_hash"] == "0" * 64
    assert after["events"][1]["previous_hash"] == after["events"][0]["event_hash"]
    assert after["head_hash"] == after["events"][-1]["event_hash"]


@pytest.mark.parametrize("operation", ["UPDATE audit SET subject='changed'", "DELETE FROM audit"])
def test_audit_table_is_append_only(registry, operation):
    registry.put("assets", {"name": "A"})
    with registry.connection() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(operation)


def test_changed_audit_event_is_detected_even_when_trigger_is_bypassed(registry):
    registry.put("assets", {"name": "A"})
    with registry.connection() as connection:
        connection.execute("DROP TRIGGER immutable_audit_update")
        connection.execute("UPDATE audit SET subject='changed'")
    with pytest.raises(IntegrityError, match="chain"):
        registry.audit()


def test_blob_is_content_addressed_and_overwrite_cannot_hide_tampering(registry):
    contents = b"original physical measurement"
    artifact = registry.store_blob(contents, "../../measurement.bin", "application/octet-stream")
    assert artifact["sha256"] == hashlib.sha256(contents).hexdigest()
    assert artifact["name"] == "measurement.bin"
    path = registry.blob_path(artifact["sha256"])
    path.write_bytes(b"modified measurement")
    with pytest.raises(IntegrityError, match="checksum"):
        registry.blob_path(artifact["sha256"])
    with pytest.raises(IntegrityError, match="checksum"):
        registry.store_blob(contents, "measurement.bin", "application/octet-stream")
    assert path.read_bytes() == b"modified measurement"


@pytest.mark.parametrize("identity", ["../registry.sqlite", "A" * 64, "z" * 64, "0" * 63])
def test_artifact_identifiers_cannot_escape_blob_storage(registry, identity):
    with pytest.raises(KeyError):
        registry.blob_path(identity)


def test_frozen_run_request_detects_storage_tampering(registry):
    frozen = {"request": {"seed": 42}}
    run = registry.submit(frozen, digest(frozen))
    with registry.connection() as connection:
        connection.execute(
            "UPDATE jobs SET document=? WHERE id=?",
            (canonical({"request": {"seed": 43}}), run["id"]),
        )
    with pytest.raises(IntegrityError, match="checksum"):
        registry.get_run(run["id"])


def test_finished_result_tampering_is_blocked_or_detected(registry):
    run = finish_fixture_run(registry)
    try:
        with registry.connection() as connection:
            connection.execute(
                "UPDATE jobs SET result=? WHERE id=?",
                (canonical({"prediction": {"values": [999]}, "artifacts": []}), run["id"]),
            )
    except sqlite3.IntegrityError:
        assert registry.get_run(run["id"])["result"] == run["result"]
    else:
        with pytest.raises(IntegrityError):
            registry.get_run(run["id"])


def test_completion_audit_digest_detects_result_changes_after_trigger_bypass(registry):
    run = finish_fixture_run(registry)
    with registry.connection() as connection:
        connection.execute("DROP TRIGGER IF EXISTS immutable_completed_job")
        connection.execute(
            "UPDATE jobs SET result=? WHERE id=?",
            (canonical({"prediction": {"values": [999]}, "artifacts": []}), run["id"]),
        )
    with pytest.raises(IntegrityError):
        registry.get_run(run["id"])


def test_failed_run_with_structured_result_detects_tampering_in_single_and_bulk_reads(registry):
    frozen = {"request": {"seed": 42}}
    queued = registry.submit(frozen, digest(frozen))
    registry.claim()
    evidence = {"resource_preflight": {"status": "blocked_working_memory_policy"}}
    registry.finish(queued["id"], result=evidence, error="Resource preflight blocked")
    assert registry.get_run(queued["id"])["result"] == evidence
    assert registry.list_runs()[0]["result"] == evidence

    with registry.connection() as connection:
        connection.execute("DROP TRIGGER IF EXISTS immutable_completed_job")
        connection.execute(
            "UPDATE jobs SET result=? WHERE id=?",
            (canonical({"resource_preflight": {"status": "ready"}}), queued["id"]),
        )
    with pytest.raises(IntegrityError, match="result failed audit checksum"):
        registry.get_run(queued["id"])
    with pytest.raises(IntegrityError, match="result failed audit checksum"):
        registry.list_runs()


def test_failed_run_must_verify_completion_event_before_trusting_its_result_hash(registry):
    frozen = {"request": {"seed": 43}}
    queued = registry.submit(frozen, digest(frozen))
    registry.claim()
    evidence = {"resource_preflight": {"status": "blocked_working_memory_policy"}}
    registry.finish(queued["id"], result=evidence, error="Resource preflight blocked")

    changed = {"resource_preflight": {"status": "ready"}}
    with registry.connection() as connection:
        connection.execute("DROP TRIGGER IF EXISTS immutable_completed_job")
        connection.execute("DROP TRIGGER IF EXISTS immutable_audit_update")
        connection.execute(
            "UPDATE jobs SET result=? WHERE id=?", (canonical(changed), queued["id"])
        )
        connection.execute(
            "UPDATE audit SET details=? WHERE subject=? AND action='runs.failed'",
            (canonical({"result_hash": digest(changed), "error": "Resource preflight blocked"}),
             queued["id"]),
        )
    with pytest.raises(IntegrityError, match="Completion audit event failed checksum"):
        registry.get_run(queued["id"])


def test_completion_event_must_be_verified_before_trusting_its_result_hash(service):
    run = finish_fixture_run(service.registry)
    changed_result = {"prediction": {"values": [999]}, "artifacts": []}
    with service.registry.connection() as connection:
        connection.execute("DROP TRIGGER IF EXISTS immutable_completed_job")
        connection.execute("DROP TRIGGER IF EXISTS immutable_audit_update")
        connection.execute(
            "UPDATE jobs SET result=? WHERE id=?", (canonical(changed_result), run["id"])
        )
        connection.execute(
            "UPDATE audit SET details=? WHERE subject=? AND action='runs.succeeded'",
            (canonical({"result_hash": digest(changed_result), "error": None}), run["id"]),
        )
    with pytest.raises(IntegrityError):
        service.export_run(run["id"])


def test_completed_run_cannot_be_refinished_with_different_evidence(registry):
    run = finish_fixture_run(registry)
    before = registry.audit()
    again = registry.finish(run["id"], result={"prediction": {"values": [999]}, "artifacts": []})
    assert again["result"] == run["result"]
    assert registry.audit() == before


def test_empty_result_has_a_real_digest_and_remains_readable(registry):
    frozen = {"request": {"seed": 42}}
    run = registry.submit(frozen, digest(frozen))
    registry.claim()
    finished = registry.finish(run["id"], result={})
    assert finished["status"] == "succeeded"
    assert finished["result"] == {}
    event = registry.audit()["events"][-1]
    assert event["details"]["result_hash"] == digest({})


def test_cancellation_wins_over_late_worker_completion(registry):
    frozen = {"request": {"seed": 42}}
    queued = registry.submit(frozen, digest(frozen))
    registry.claim()
    registry.cancel(queued["id"])
    result = registry.finish(
        queued["id"], result={"prediction": {"values": [999]}, "artifacts": []}
    )
    assert result["status"] == "cancelled"
    assert result["result"] is None


def test_interrupted_running_job_is_failed_with_frozen_inputs_preserved(registry):
    frozen = {"request": {"seed": 42}}
    run = registry.submit(frozen, digest(frozen))
    registry.claim()
    registry.recover_interrupted()
    recovered = registry.get_run(run["id"])
    assert recovered["status"] == "failed"
    assert recovered["request"] == frozen["request"]
    assert "interrupted" in recovered["error"].lower()
    assert registry.list_runs()[0]["result"] is None


def test_identical_inputs_reuse_cache_but_force_produces_new_run(registry):
    frozen = {"request": {"seed": 42}}
    first = registry.submit(frozen, digest(frozen))
    cached = registry.submit(deepcopy(frozen), digest(frozen))
    forced = registry.submit(frozen, digest(frozen), force=True)
    assert cached["id"] == first["id"]
    assert cached["cache_hit"]
    assert forced["id"] != first["id"]


def test_cache_key_cannot_reuse_an_unrelated_frozen_request(registry):
    first = {"request": {"seed": 42}}
    registry.submit(first, digest(first))
    with pytest.raises((IntegrityError, ConflictError, ValueError)):
        registry.submit({"request": {"seed": 999}}, digest(first))


def test_service_cache_identity_tracks_seed_and_test_conditions(service):
    assembly = service.registry.list("assemblies")[0]
    test = service.registry.list("tests")[0]
    request = {
        "assembly_id": assembly["id"],
        "test_id": test["id"],
        "plugin_id": "response_db",
        "parameters": {"seed": 42},
    }
    first = service.submit_run(request)
    repeat = service.submit_run(deepcopy(request))
    assert repeat["id"] == first["id"]
    different_seed = service.submit_run({**request, "parameters": {"seed": 43}})
    different_exposure = service.submit_run(
        {**request, "parameters": {"seed": 42, "exposure_ms": 8.0}}
    )
    assert (
        len({first["cache_key"], different_seed["cache_key"], different_exposure["cache_key"]}) == 3
    )
    assert different_exposure["test_snapshot"]["conditions"]["exposure_s"]["value"] == 0.008
    assert service.registry.get("tests", test["id"])["conditions"]["exposure_s"]["value"] == 0.004


def test_worker_rejects_source_or_environment_drift_before_execution(service, monkeypatch):
    import camera_platform.service as service_module

    job = {"environment": {"source_hashes": {"runtime.py": "old"}}}
    monkeypatch.setattr(
        service_module, "environment_fingerprint", lambda: {"source_hashes": {"runtime.py": "new"}}
    )
    with pytest.raises(ValueError, match="differs"):
        service.execute_run(job)


def test_export_checks_referenced_artifact_bytes(service):
    artifact = service.registry.store_blob(b"raw bytes", "raw.npy", "application/octet-stream")
    run = finish_fixture_run(service.registry, result={"prediction": {}, "artifacts": [artifact]})
    service.registry.blob_path(artifact["sha256"]).write_bytes(b"tampered bytes")
    with pytest.raises(IntegrityError, match="checksum"):
        service.export_run(run["id"])


def test_export_does_not_silently_publish_a_tampered_result(service):
    run = finish_fixture_run(service.registry)
    try:
        with service.registry.connection() as connection:
            connection.execute("UPDATE jobs SET result='{}' WHERE id=?", (run["id"],))
    except sqlite3.IntegrityError:
        assert service.export_run(run["id"])["run"]["result"] == run["result"]
    else:
        with pytest.raises(IntegrityError):
            service.export_run(run["id"])


def test_measured_dataset_original_is_frozen_and_duplicate_identity_rejected(service):
    payload = measurement_payload(service)
    measurement = service.create_measurement(payload)
    artifact = measurement["original_artifact"]
    original_bytes = service.registry.blob_path(artifact["sha256"]).read_bytes()
    original = json.loads(original_bytes)
    assert original["values"] == payload["values"]
    assert original["dataset_ids"] == payload["dataset_ids"]
    assert original["instrument"] == payload["instrument"]
    assert original["physical_instance"] == "camera-serial-42"
    payload["values"][0] = 999
    with pytest.raises(ConflictError):
        service.create_measurement(payload)
    assert service.registry.blob_path(artifact["sha256"]).read_bytes() == original_bytes
    assert service.registry.get("measurements", measurement["id"])["values"][0] == 1.0


def test_measurement_source_bytes_preserve_original_json_encoding(service):
    payload = measurement_payload(service)
    original_bytes = (json.dumps(payload, ensure_ascii=False, indent=3) + "\n\n").encode("utf-8")
    measurement = service.create_measurement(payload, original_bytes=original_bytes)
    artifact = measurement["original_artifact"]
    assert measurement["original_encoding"] == "verbatim_utf8_json"
    assert artifact["sha256"] == hashlib.sha256(original_bytes).hexdigest()
    assert service.registry.blob_path(artifact["sha256"]).read_bytes() == original_bytes


def test_measurement_import_rejects_original_bytes_for_a_different_dataset(service):
    payload = measurement_payload(service)
    different = deepcopy(payload)
    different["values"][0] = 999
    with pytest.raises(ValueError, match="do not match"):
        service.create_measurement(payload, original_bytes=json.dumps(different).encode())
    assert service.registry.list("measurements") == []


def test_derived_model_cannot_erase_ancestor_calibration_datasets(service):
    measurement = service.create_measurement(measurement_payload(service, "training:camera-A"))
    model = next(m for m in service.registry.list("models") if m["kind"] == "lens")
    keys = set(service.model_contract.model_fields)
    parent_payload = {key: deepcopy(value) for key, value in model.items() if key in keys}
    parent_payload.update(
        name="Calibrated parent", calibration_dataset_ids=measurement["dataset_ids"]
    )
    parent = service.create_model(parent_payload)
    child_payload = {key: deepcopy(value) for key, value in parent.items() if key in keys}
    child_payload.update(name="Derived child", parents=[parent["id"]], calibration_dataset_ids=[])
    try:
        child = service.create_model(child_payload)
    except ValueError:
        assert service.registry.get("models", parent["id"])["calibration_dataset_ids"] == [
            "training:camera-A"
        ]
    else:
        assert "training:camera-A" in child["calibration_dataset_ids"]


def test_release_stays_unapproved_without_independent_physical_validation(service):
    assembly = service.registry.list("assemblies")[0]
    models = [service.registry.get("models", identity) for identity in assembly["model_ids"]]
    run = finish_fixture_run(
        service.registry,
        document={
            "assembly_id": assembly["id"],
            "model_snapshots": models,
            "request": {"seed": 42},
        },
    )
    release = service.create_release({"name": "Review package", "run_ids": [run["id"]]})
    assert release["status"] == "blocked"
    assert release["approval_status"] == "unapproved"
    assert "independent_validation_missing" in {blocker["code"] for blocker in release["blockers"]}


def test_release_export_carries_measurements_and_verifies_the_original_blob(service):
    measurement = service.create_measurement(measurement_payload(service))
    assembly = service.registry.get("assemblies", measurement["assembly_id"])
    models = [service.registry.get("models", identity) for identity in assembly["model_ids"]]
    prediction = {
        **deepcopy(measurement),
        "source_kind": "simulated",
        "method_and_version": "test-only-solver:1",
        "uncertainty": {"kind": "unknown"},
        "calibration_dataset_ids": [],
    }
    run = finish_fixture_run(
        service.registry,
        document={
            "assembly_id": assembly["id"],
            "test_id": measurement["test_id"],
            "model_snapshots": models,
            "request": {"seed": 42},
        },
        result={"prediction": prediction, "artifacts": []},
    )
    report = service.validate(
        {
            "run_id": run["id"],
            "measurement_id": measurement["id"],
            "test_id": measurement["test_id"],
        }
    )
    assert not report["physical_validation_pass"]
    release = service.create_release(
        {"name": "Incomplete evidence", "run_ids": [run["id"]], "validation_ids": [report["id"]]}
    )
    package = service.export_release(release["id"])
    assert package["measurements"] == [measurement]
    artifact = measurement["original_artifact"]
    service.registry.blob_path(artifact["sha256"]).write_bytes(b"changed measurement")
    with pytest.raises(IntegrityError, match="checksum"):
        service.export_release(release["id"])
