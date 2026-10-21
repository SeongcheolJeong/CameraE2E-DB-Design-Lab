"""Display transport must preserve evidence identity and live worker state."""

from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.registry import IntegrityError, SQLiteRegistry, digest
from camera_platform.sdk import PlatformClient
from camera_platform.workbench import bootstrap, detail, project


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / "api", start_worker=False)
    with TestClient(app) as client:
        yield app.state.service, client


@pytest.mark.parametrize(
    "collection,kind",
    [
        ("runs", None),
        ("factory_jobs", "characterization"),
        ("replays", "isp_replay"),
        ("surrogates", "surrogate_training"),
    ],
)
def test_projection_and_detail_preserve_exact_job_evidence(api, collection, kind):
    service, client = api
    request = {"name": "원본", "model_snapshots": ["large evidence" * 2000]}
    if kind:
        request["job_kind"] = kind
    job = service.registry.submit(request, digest(request))
    queued = project(collection, job)
    assert "result" not in queued and "model_snapshots" not in queued
    service.registry.claim()
    service.registry.finish(
        job["id"],
        result={
            "artifacts": [],
            "channel_ratios": [[1.0, 2.0]],
            "frames": [{"original": "frame evidence" * 1000}],
        },
    )
    full = service.registry.get_run(job["id"])
    index = client.get("/api/v1/workbench").json()
    projected = next(r for r in index[collection] if r["id"] == job["id"])
    assert projected["_projection"]["record_sha256"] == digest(full)
    assert queued["_projection"]["record_sha256"] != digest(full)
    assert projected["display_summary"]["frame_count"] == 1
    assert projected["display_summary"]["channel_ratios"] == [[1.0, 2.0]]
    response = client.get("/api/v1" + projected["_projection"]["detail_path"])
    assert response.status_code == 200
    assert response.json()["record"] == full
    assert response.json()["record_sha256"] == digest(full)
    assert (
        next(r for r in client.get("/api/v1/bootstrap").json()[collection] if r["id"] == job["id"])
        == full
    )
    assert len(str(projected)) < len(str(full)) / 10
    other = "replays" if collection != "replays" else "runs"
    assert client.get(f"/api/v1/workbench/{other}/{job['id']}").status_code == 404


def test_model_and_prediction_details_do_not_substitute_display_summaries(api):
    service, client = api
    model = service.registry.list("models")[0]
    projection = project("models", model)
    assert "payload" not in projection and "payload" in model
    assert detail(service, "models", model["id"])["record"] == model
    prediction = service.registry.put(
        "surrogate_predictions",
        {
            "model_id": "source",
            "rows": [{"output": x / 3} for x in range(1024)],
        },
    )
    projection = project("surrogate_predictions", prediction)
    assert projection["display_summary"]["point_count"] == 1024
    assert "rows" not in projection
    assert detail(service, "surrogate_predictions", prediction["id"])["record"] == prediction
    assert client.get("/api/v1/workbench/unknown/anything").status_code == 404


@pytest.mark.parametrize("frames,count", [(4, 4), ([{}, {}], 2), (None, None)])
def test_frame_count_preserves_legacy_scalar_and_sequence_contracts(frames, count):
    record = {"id": "run-legacy", "result": {"frames": frames}}
    assert project("runs", record)["display_summary"]["frame_count"] == count


def test_bootstrap_has_one_verified_job_pass_and_no_missing_records(api, monkeypatch):
    service, _ = api
    original = service.registry.list_runs
    calls = []

    def read():
        calls.append(True)
        return original()

    monkeypatch.setattr(service.registry, "list_runs", read)
    index = bootstrap(service)
    assert len(calls) == 1
    for collection, count in index["counts"].items():
        assert len(index[collection]) == count
    assert index["workbench_schema"] == "camera_workbench_v1"


def test_registry_bulk_read_uses_one_snapshot_and_still_checks_audit(tmp_path, monkeypatch):
    registry = SQLiteRegistry(tmp_path)
    for i in range(12):
        job = registry.submit({"name": str(i)}, digest({"name": str(i)}))
        registry.claim()
        registry.finish(job["id"], result={"value": i})
    original = registry.connection
    queries = []
    connections = []

    @contextmanager
    def connection():
        connections.append(True)
        with original() as conn:
            conn.set_trace_callback(queries.append)
            yield conn

    monkeypatch.setattr(registry, "connection", connection)
    jobs = registry.list_runs()
    assert len(jobs) == 12 and len(connections) == 1
    assert "BEGIN" in queries
    assert sum(q.startswith("SELECT") for q in queries) == 2
    assert jobs == [registry.get_run(r["id"]) for r in jobs]
    with original() as conn:
        conn.execute("DROP TRIGGER immutable_audit_update")
        conn.execute("UPDATE audit SET details='{}' WHERE action='runs.succeeded'")
    with pytest.raises(IntegrityError, match="audit event"):
        registry.list_runs()


def test_session_is_small_and_has_same_origin_protection(api, monkeypatch):
    service, client = api
    monkeypatch.setattr(service, "bootstrap", lambda: pytest.fail("session read all data"))
    response = client.get("/api/v1/session")
    assert response.status_code == 200 and len(response.content) < 1024
    assert response.json()["schema"] == "camera_platform_v1"
    for endpoint in ("session", "workbench"):
        response = client.get("/api/v1/" + endpoint, headers={"origin": "https://example.com"})
        assert response.status_code == 403 and "token" not in response.json()


@pytest.mark.parametrize(
    "status,paths",
    [
        (200, ["/api/v1/session"]),
        (404, ["/api/v1/session", "/api/v1/bootstrap"]),
        (403, ["/api/v1/session"]),
    ],
)
def test_sdk_session_and_only_404_legacy_fallback(monkeypatch, status, paths):
    requested = []

    def respond(request):
        requested.append(request.url.path)
        code = status if request.url.path.endswith("session") else 200
        return httpx.Response(code, json={"schema": "camera_platform_v1", "token": "test"})

    http = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: http)
    if status == 403:
        with pytest.raises(httpx.HTTPStatusError):
            PlatformClient()
    else:
        with PlatformClient() as sdk:
            assert sdk.http.headers["X-Platform-Token"] == "test"
    assert requested == paths and http.is_closed
