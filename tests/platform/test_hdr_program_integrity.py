"""Missing or corrupt electrical program evidence blocks submit, cache and Worker."""

import pytest
from test_hdr_workflow import finish
from test_hdr_workflow import workflow as workflow

from camera_platform.hdr_runtime import HDRBracketRuntime
from camera_platform.registry import IntegrityError
from camera_platform.worker import execute


@pytest.mark.parametrize("artifact_name", [
    "capture-program-source.json", "capture-program-normalized.json",
])
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
@pytest.mark.parametrize("stage", ["initial_submit", "succeeded_cache", "queued_worker"])
def test_program_evidence_is_required_at_every_execution_boundary(
    workflow, monkeypatch, artifact_name, damage, stage
):
    service, _, _, _, _, program, _, request = workflow
    completed = queued = None
    if stage == "succeeded_cache":
        completed = finish(service, service.submit_run(request))
        assert service.submit_run(request)["id"] == completed["id"]
    elif stage == "queued_worker":
        queued = service.submit_run(request)

    artifact = next(a for a in program["artifacts"] if a["name"] == artifact_name)
    path = service.registry.blob_path(artifact["sha256"])
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupted owned test program evidence")

    monkeypatch.setattr(
        HDRBracketRuntime, "execute", lambda *a, **kw: pytest.fail("Damaged evidence reached RAW")
    )
    error_type = KeyError if damage == "missing" else IntegrityError
    expected_error = "Artifact not found" if damage == "missing" else "Artifact checksum mismatch"
    before_ids = [run["id"] for run in service.registry.list_runs()]
    if stage == "queued_worker":
        claimed = service.registry.claim()
        assert claimed["id"] == queued["id"]
        execute(service, claimed["id"])
        failed = service.registry.get_run(claimed["id"])
        assert failed["status"] == "failed"
        assert expected_error in failed["error"]
        assert failed["result"] is None
    else:
        with pytest.raises(error_type, match=expected_error):
            service.submit_run(request)
        if completed is not None:
            # Refusing a new cache request never rewrites its historical result.
            assert service.registry.get_run(completed["id"]) == completed
    assert [run["id"] for run in service.registry.list_runs()] == before_ids
    assert service.registry.get("capture_programs", program["id"]) == program
    assert service.registry.audit()["integrity"] == "verified"
