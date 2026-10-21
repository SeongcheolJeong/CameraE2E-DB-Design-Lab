"""Request-local reuse must preserve fresh evidence and immutable saved cases."""

from collections import Counter
from copy import deepcopy

import pytest
from test_isp_studies import complete, reference, rows, submit
from test_isp_studies import prepared as prepared
from test_isp_studies import source as source

from camera_platform import case_reviews
from camera_platform import isp_studies as studies
from camera_platform.registry import IntegrityError, canonical, digest
from camera_platform.worker import execute


@pytest.fixture
def completed(prepared):
    service, raw, _, _, baseline, spec = prepared
    metadata, content = reference(service, raw, baseline)
    independent = studies.import_reference(service, metadata, content)
    spec["datasets"][0]["reference_id"] = independent["id"]
    study = submit(prepared)
    analysis = complete(service, study)
    selection = studies.select_candidate(service, study["id"], {
        "candidate_id": study["candidates"][0]["candidate_id"],
        "rationale": "Preserve the baseline while checking independent evidence reads",
        "analysis_sha256": analysis["analysis_sha256"],
    })
    return service, raw, independent, study, analysis, selection


def readers(service, study, selection):
    return {
        "study": lambda: studies.get_study(service, study["id"]),
        "analysis": lambda: studies.analyze(service, study["id"]),
        "selection": lambda: studies.get_selection(service, selection["id"]),
        "export": lambda: studies.export_manifest(service, study["id"]),
    }


@pytest.mark.parametrize("fault", ["raw", "reference", "calibration", "output"])
def test_every_public_read_rechecks_same_size_corrupted_blobs(completed, fault):
    service, raw, independent, study, _, selection = completed
    operations = readers(service, study, selection)
    before = {name: operation() for name, operation in operations.items()}
    cell = service.registry.get_run(study["cells"][0]["job_id"])
    descriptor = {
        "raw": raw["raw_artifact"],
        "reference": independent["array_artifact"],
        "calibration": study["calibration_snapshot"]["artifacts"][0],
        "output": next(a for a in cell["artifacts"] if a["name"] == "sharpened_rgb_frames.npy"),
    }[fault]
    path = service.registry.blob_path(descriptor["sha256"])
    original = path.read_bytes()
    assert original
    path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    try:
        for operation in operations.values():
            with pytest.raises(IntegrityError):
                operation()
    finally:
        path.write_bytes(original)
    assert {name: operation() for name, operation in operations.items()} == before


@pytest.mark.parametrize("fault", ["source_request", "model_record", "cell_result", "cell_audit"])
def test_every_public_read_rechecks_registry_and_completion_proofs(completed, fault):
    service, raw, _, study, _, selection = completed
    operations = readers(service, study, selection)
    for operation in operations.values():
        operation()
    cell_id = study["cells"][0]["job_id"]
    with service.registry.transaction() as conn:
        if fault == "model_record":
            identity = study["candidates"][0]["isp_model_id"]
            row = conn.execute("SELECT document FROM records WHERE id=?", (identity,)).fetchone()
            value = service.registry.decode_document(row["document"])
            value["name"] += " modified after first read"
            conn.execute("DROP TRIGGER immutable_records_update")
            conn.execute("UPDATE records SET document=? WHERE id=?", (canonical(value), identity))
        elif fault == "cell_audit":
            conn.execute("DROP TRIGGER immutable_audit_update")
            conn.execute(
                "UPDATE audit SET details=? WHERE subject=? AND action='runs.succeeded'",
                (canonical({"result_hash": "0" * 64}), cell_id),
            )
        else:
            identity = raw["source_id"] if fault == "source_request" else cell_id
            field = "document" if fault == "source_request" else "result"
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
            value = service.registry.decode_document(row[field])
            value["changed_after_verified_read"] = True
            conn.execute("DROP TRIGGER immutable_completed_job")
            conn.execute(
                "UPDATE jobs SET " + field + "=? WHERE id=?", (canonical(value), identity)
            )
    for operation in operations.values():
        with pytest.raises(IntegrityError):
            operation()


def test_next_public_read_observes_partial_then_complete_worker_results(prepared):
    service, *_ = prepared
    study = submit(prepared)
    queued = studies.export_manifest(service, study["id"])
    assert queued["analysis"]["coverage"]["queued"] == 2
    first = service.registry.claim()
    assert first["id"] == study["cells"][0]["job_id"]
    execute(service, first["id"])
    partial = studies.export_manifest(service, study["id"])
    assert partial["analysis"]["coverage"] == {
        "queued": 1, "running": 0, "succeeded": 1, "failed": 0, "cancelled": 0,
    }
    second = service.registry.claim()
    assert second["id"] == study["cells"][1]["job_id"]
    execute(service, second["id"])
    complete_manifest = studies.export_manifest(service, study["id"])
    assert complete_manifest["analysis"]["coverage"]["succeeded"] == 2
    assert len({
        value["analysis"]["analysis_sha256"] for value in (queued, partial, complete_manifest)
    }) == 3
    for manifest in (queued, partial, complete_manifest):
        assert manifest["study"] == study
        assert [cell["status"] for cell in manifest["cells"]] == [
            row["status"] for row in manifest["analysis"]["rows"]
        ]
    assert [cell["status"] for cell in queued["cells"]] == ["queued", "queued"]


def test_export_and_selection_verify_each_cell_once_per_public_read(completed, monkeypatch):
    service, _, _, study, _, selection = completed
    counts = Counter()
    original_inputs, original_cell = studies._inputs, studies._cell

    def inputs(service, value):
        counts["inputs"] += 1
        return original_inputs(service, value)

    def cell(service, value, specification, **kwargs):
        counts[specification["job_id"]] += 1
        return original_cell(service, value, specification, **kwargs)

    monkeypatch.setattr(studies, "_inputs", inputs)
    monkeypatch.setattr(studies, "_cell", cell)
    for operation in (
        lambda: studies.export_manifest(service, study["id"]),
        lambda: studies.get_selection(service, selection["id"]),
    ):
        counts.clear()
        operation()
        assert counts == Counter({"inputs": 1, **{c["job_id"]: 1 for c in study["cells"]}})


def test_public_reads_remain_detached_and_preserve_complete_manifest(completed):
    service, _, _, study, analysis, selection = completed
    cells = [studies.get_cell(service, cell["job_id"]) for cell in study["cells"]]
    expected = {
        "schema": "camera_isp_study_evidence_v1", "study": study, "analysis": analysis,
        "cells": cells, **study["plan_hash_basis"]["provenance_closure"], **studies.AUTHORITY,
    }
    expected["artifacts"] = studies._verify_artifacts(service, expected)
    before = rows(service)
    manifest = studies.export_manifest(service, study["id"])
    assert canonical(manifest) == canonical(expected)
    assert digest(manifest) == digest(expected)
    manifest["study"]["name"] = "Caller mutation"
    manifest["cells"][0]["result"].clear()
    changed_selection = studies.get_selection(service, selection["id"])
    changed_selection["analysis_snapshot"]["rows"].clear()
    assert studies.export_manifest(service, study["id"]) == expected
    assert studies.get_selection(service, selection["id"]) == selection
    assert rows(service) == before


def test_case_keeps_full_export_and_selection_snapshots_without_repeated_cell_reads(
    completed, monkeypatch
):
    service, raw, _, study, _, selection = completed
    manifest = studies.export_manifest(service, study["id"])
    dataset_key = study["datasets"][0]["key"]
    original = raw["source_snapshot"]
    draft = {
        "request_id": "isp-evidence-read-boundaries", "name": "Exact retained RAW evidence",
        "question": "Does this fixed acquisition support the selected ISP profile?",
        "requirements": [{"key": "noise", "description": "Fixed noise condition",
                          "test_id": original["test_id"]}],
        "assembly_ids": [original["assembly_id"]],
        "evidence": [{"kind": "isp_studies", "id": study["id"],
                      "requirement_keys": ["noise"],
                      "dataset_requirements": {dataset_key: ["noise"]},
                      "selection_id": selection["id"]}],
        "decision": {"status": "investigate", "rationale": "Numerical evidence only"},
    }
    counts = Counter()
    original_cell = studies._cell

    def cell(service, value, specification, **kwargs):
        counts[specification["job_id"]] += 1
        return original_cell(service, value, specification, **kwargs)

    monkeypatch.setattr(studies, "_cell", cell)
    saved = case_reviews.create_case_review(service, draft)
    expected_counts = Counter({c["job_id"]: 1 for c in study["cells"]})
    assert counts == expected_counts
    export_record = {"id": study["id"], "manifest": manifest}
    evidence = saved["snapshots"]["evidence"][0]
    assert evidence["snapshot"] == study
    assert evidence["record_sha256"] == digest(study)
    assert evidence["linked_records"] == [
        {"kind": "isp_study_exports", "id": study["id"],
         "record_sha256": digest(export_record), "snapshot": export_record},
        {"kind": "isp_selections", "id": selection["id"],
         "record_sha256": digest(selection), "snapshot": selection},
    ]
    frozen_hash, frozen_copy = digest(saved), deepcopy(saved)
    counts.clear()
    assert case_reviews.get_case_review(service, saved["id"]) == frozen_copy
    assert counts == expected_counts
    assert digest(case_reviews.get_case_review(service, saved["id"])) == frozen_hash
