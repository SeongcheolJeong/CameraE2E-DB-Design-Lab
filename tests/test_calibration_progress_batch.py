"""Bound operational polling and batch evidence reads retain integrity checks."""

from types import SimpleNamespace

import pytest

from camera_platform import calibration_lab as lab
from camera_platform.registry import IntegrityError, SQLiteRegistry, canonical, digest


def queued_lab(registry, kind=lab.JOB_KIND):
    request = {"job_kind": kind, "specification": {"campaign_id": "campaign-test"}}
    return registry.submit(request, digest(request), force=True)


def test_operational_state_does_not_decode_evidence_and_binds_expected_request(
    tmp_path, monkeypatch
):
    registry = SQLiteRegistry(tmp_path)
    job = queued_lab(registry)
    monkeypatch.setattr(
        registry, "decode_document", lambda *_: pytest.fail("State decoded evidence")
    )
    state = registry.get_run_state(job["id"], job["content_hash"])
    assert state["status"] == "queued"
    assert state["scope"] == "operational_state_only_not_evidence_verification"
    with pytest.raises(IntegrityError, match="expected request hash"):
        registry.get_run_state(job["id"], "f" * 64)
    with pytest.raises(KeyError):
        registry.get_run_state("missing")


def test_progress_identifies_calibration_once_then_reads_live_state(tmp_path, monkeypatch):
    registry = SQLiteRegistry(tmp_path)
    service = SimpleNamespace(registry=registry)
    job = queued_lab(registry)
    first = lab.operational_progress(service, job["id"], job["content_hash"])
    assert first["campaign_id"] == "campaign-test"
    assert first["job_kind"] == lab.JOB_KIND
    directory = lab._directory(service, job["id"])
    directory.mkdir(parents=True)
    (directory / "progress.json").write_text(canonical({"fraction": 0.25}))
    monkeypatch.setattr(registry, "get_run", lambda *_: pytest.fail("Repeated full job decode"))
    with registry.connection() as connection:
        connection.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job["id"],))
    second = lab.operational_progress(service, job["id"], job["content_hash"])
    assert second["status"] == "cancelled"
    assert second["progress"] == {"fraction": 0.25}
    assert "result" not in second and "measurement_snapshots" not in second
    with pytest.raises(IntegrityError):
        lab.operational_progress(service, job["id"], "f" * 64)


def test_progress_does_not_admit_other_job_kinds(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    job = queued_lab(registry, kind="unrelated_solver")
    service = SimpleNamespace(registry=registry)
    with pytest.raises(ValueError, match="not a calibration"):
        lab.operational_progress(service, job["id"], job["content_hash"])
    with pytest.raises(ValueError, match="known study request hash"):
        lab.operational_progress(service, job["id"], "invalid")


def finished_owner(registry):
    artifacts = [
        registry.store_blob(bytes([n]) * 20, f"image-{n}.png", "image/png") for n in range(8)
    ]
    job = queued_lab(registry)
    registry.claim()
    registry.finish(job["id"], result={"artifacts": artifacts})
    return job, artifacts


def test_batch_verifies_owner_and_descriptor_list_once_and_every_blob(tmp_path, monkeypatch):
    registry = SQLiteRegistry(tmp_path)
    job, artifacts = finished_owner(registry)
    calls = {"owner": 0, "rows": 0, "blob": 0}
    decode = registry._decode_job
    rows = registry.artifact_rows
    blob = registry.blob_path

    def count_decode(*args, **kwargs):
        calls["owner"] += 1
        return decode(*args, **kwargs)

    def count_rows(*args, **kwargs):
        calls["rows"] += 1
        return rows(*args, **kwargs)

    def count_blob(*args, **kwargs):
        calls["blob"] += 1
        return blob(*args, **kwargs)

    monkeypatch.setattr(registry, "_decode_job", count_decode)
    monkeypatch.setattr(registry, "artifact_rows", count_rows)
    monkeypatch.setattr(registry, "blob_path", count_blob)
    result = registry.artifact_bindings(
        [a["id"] for a in artifacts], owner_kind="runs", owner_id=job["id"]
    )
    assert calls == {"owner": 1, "rows": 1, "blob": 8}
    for artifact in artifacts:
        assert result[artifact["id"]]["descriptor"] == artifact
    # A fresh second operation must reverify the owner and all actual bytes.
    registry.artifact_bindings([a["id"] for a in artifacts], owner_kind="runs", owner_id=job["id"])
    assert calls == {"owner": 2, "rows": 2, "blob": 16}


@pytest.mark.parametrize("damage", ["blob", "head", "descriptor", "result", "wrong_owner"])
def test_batch_rejects_byte_owner_descriptor_head_and_audit_damage(tmp_path, damage):
    registry = SQLiteRegistry(tmp_path)
    job, artifacts = finished_owner(registry)
    identity = artifacts[-1]["id"]
    if damage == "blob":
        registry.blob_path(identity).write_bytes(b"x" * 20)
    elif damage == "wrong_owner":
        other = queued_lab(registry)
        with pytest.raises(KeyError, match="exact owner"):
            registry.artifact_bindings([identity], owner_kind="runs", owner_id=other["id"])
        return
    else:
        with registry.connection() as connection:
            if damage == "head":
                connection.execute(
                    "UPDATE artifact_heads SET binding_hash=? WHERE artifact_id=?",
                    ("f" * 64, identity),
                )
            elif damage == "descriptor":
                connection.execute(
                    "UPDATE artifact_owners SET descriptor_hash=? WHERE artifact_id=?",
                    ("f" * 64, identity),
                )
                forged = dict(
                    connection.execute(
                        "SELECT * FROM artifact_owners WHERE artifact_id=?", (identity,)
                    ).fetchone()
                )
                connection.execute(
                    "UPDATE artifact_heads SET binding_hash=? WHERE artifact_id=?",
                    (digest(forged), identity),
                )
            else:
                connection.execute("DROP TRIGGER immutable_completed_job")
                connection.execute(
                    "UPDATE jobs SET result=? WHERE id=?",
                    (canonical({"artifacts": artifacts, "tampered": True}), job["id"]),
                )
    with pytest.raises(IntegrityError):
        registry.artifact_bindings(
            [a["id"] for a in artifacts], owner_kind="runs", owner_id=job["id"]
        )


def test_batch_record_owner_checks_creation_audit_and_rejects_unrelated_artifacts(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    artifact = registry.store_blob(b"input", "capture.csv", "text/csv")
    owner = registry.put("measurements", {"original_artifact": artifact})
    bound = registry.artifact_bindings(
        [artifact["id"]], owner_kind="measurements", owner_id=owner["id"]
    )
    assert bound[artifact["id"]]["owner"] == owner
    with registry.connection() as connection:
        connection.execute("DROP TRIGGER immutable_audit_delete")
        connection.execute("DELETE FROM audit WHERE subject=?", (owner["id"],))
    with pytest.raises(IntegrityError, match="creation audit"):
        registry.artifact_bindings(
            [artifact["id"]], owner_kind="measurements", owner_id=owner["id"]
        )
