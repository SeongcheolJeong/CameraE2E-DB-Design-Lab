"""Lab views avoid decoding unrelated historical result payloads."""

from camera_platform.registry import SQLiteRegistry, digest


def test_filtered_jobs_preserve_integrity_without_decoding_other_results(tmp_path, monkeypatch):
    registry = SQLiteRegistry(tmp_path)
    for kind in ("other_expensive_solver", "calibration_lab"):
        request = {"job_kind": kind}
        queued = registry.submit(request, digest(request))
        registry.claim()
        registry.finish(queued["id"], result={"kind": kind, "artifacts": []})
    with registry.connection() as conn:
        unrelated = next(
            row["result"]
            for row in conn.execute("SELECT * FROM jobs")
            if registry.decode_document(row["document"])["job_kind"] == "other_expensive_solver"
        )
    original = registry.decode_document

    def decode(value):
        assert value != unrelated, "Filtered listing must not load unrelated result blobs"
        return original(value)

    monkeypatch.setattr(registry, "decode_document", decode)
    runs = registry.list_runs(job_kinds={"calibration_lab"})
    assert len(runs) == 1
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["result"]["kind"] == "calibration_lab"
