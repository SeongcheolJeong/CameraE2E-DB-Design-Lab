"""Primary admission, uncertain submissions, append preservation and real recipe tests.

No primary state or listening server is used. The recipe test uses an ephemeral
TestClient and the actual service calculation path without a detached Worker.
"""

import copy
import importlib.util
import io
import json
import sqlite3
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
SPEC = importlib.util.spec_from_file_location(
    "primary_delivery", TOOLS / "verify_camera_prescription_delivery.py"
)
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)


class MemoryClient:
    def __init__(self):
        self.records = {}
        self.posts = []
        self.lose_response = False
        self.http = None

    def get(self, path):
        if path == "models":
            return list(self.records.values())
        return copy.deepcopy(self.records[path.split("/")[-1]])

    def post(self, path, payload):
        self.posts.append((path, payload))
        identity = "saved-" + str(len(self.posts))
        self.records[identity] = {"id": identity, **payload}
        if self.lose_response:
            raise TimeoutError("response lost after accepted mutation")
        return self.records[identity]


def reopen(journal, *, read_only=False):
    journal.lock.close()
    return delivery.Journal(journal.output, journal.data["inputs"], read_only=read_only)


def test_uncertain_post_reconciles_exact_record_without_resubmitting(tmp_path):
    journal = delivery.Journal(tmp_path / "receipt.json", {"fixed": True})
    client = MemoryClient()
    client.lose_response = True
    with pytest.raises(TimeoutError):
        journal.acquire(
            client, "sensor", "models", "models", {"name": "frozen", "payload": {"x": 1}}
        )
    saved = json.loads(journal.output.read_text())
    assert saved["operations"]["sensor"]["status"] == "submitting"
    assert saved["latest_invocation"]["post_count"] == 1
    journal = reopen(journal)
    record = journal.acquire(
        client, "sensor", "models", "models", {"name": "frozen", "payload": {"x": 1}}
    )
    assert record["id"] == "saved-1" and len(client.posts) == 1
    assert journal.data["latest_invocation"]["post_count"] == 0
    assert journal.data["operations"]["sensor"]["status"] == "reconciled"
    journal.lock.close()


@pytest.mark.parametrize("matches", [0, 2])
def test_ambiguous_or_unseen_submission_is_never_retried(tmp_path, matches):
    journal = delivery.Journal(tmp_path / "receipt.json", {})
    client = MemoryClient()
    client.lose_response = True
    with pytest.raises(TimeoutError):
        journal.acquire(client, "model", "models", "models", {"name": "frozen"})
    client.records = {str(i): {"id": str(i), "name": "frozen"} for i in range(matches)}
    journal = reopen(journal)
    with pytest.raises(RuntimeError, match="no resubmission"):
        journal.acquire(client, "model", "models", "models", {"name": "frozen"})
    assert len(client.posts) == 1
    journal.lock.close()


@pytest.mark.parametrize("change", ["intent", "record", "missing-intent"])
def test_saved_identity_cannot_hide_changed_request_or_record(tmp_path, change):
    journal = delivery.Journal(tmp_path / "receipt.json", {})
    client = MemoryClient()
    journal.acquire(client, "model", "models", "models", {"name": "frozen"})
    payload = {"name": "frozen"}
    if change == "intent":
        payload["name"] = "new"
    elif change == "record":
        client.records["saved-1"]["name"] = "different"
    else:
        del journal.data["operations"]["model"]
    with pytest.raises(RuntimeError):
        journal.acquire(client, "model", "models", "models", payload)
    assert len(client.posts) == 1
    journal.lock.close()


def test_read_only_missing_id_refuses_submission(tmp_path):
    journal = delivery.Journal(tmp_path / "receipt.json", {})
    journal.data["first_verified_at"] = delivery.stamp()
    journal.save()
    journal = reopen(journal, read_only=True)
    client = MemoryClient()
    with pytest.raises(RuntimeError, match="Read-only"):
        journal.acquire(client, "missing", "models", "models", {"name": "new"})
    assert not client.posts
    journal.lock.close()


def test_journal_lock_and_inputs_prevent_concurrent_or_misbound_resume(tmp_path):
    path = tmp_path / "receipt.json"
    journal = delivery.Journal(path, {"version": "fixed"})
    with pytest.raises(BlockingIOError):
        delivery.Journal(path, {"version": "fixed"})
    journal.lock.close()
    with pytest.raises(RuntimeError, match="different inputs"):
        delivery.Journal(path, {"version": "other"})


def test_retry_archives_old_failure_in_invocation(tmp_path):
    journal = delivery.Journal(tmp_path / "receipt.json", {})
    journal.data["failure"] = "prior timeout"
    journal.save()
    journal = reopen(journal)
    assert "failure" not in journal.data
    assert journal.data["latest_invocation"]["previous_failure"] == "prior timeout"
    journal.lock.close()


@pytest.mark.parametrize(
    "path,payload", [("runs/one/cancel", {}), ("models/one", {}), ("runs", {"force": True})]
)
def test_delivery_client_never_admits_destructive_or_forced_endpoints(path, payload):
    client = MemoryClient()
    guarded = delivery.GuardedClient(client, lambda: pytest.fail("guard should not be reached"))
    with pytest.raises(RuntimeError):
        guarded.post(path, payload)
    assert not client.posts


def test_failed_live_guard_prevents_post():
    client = MemoryClient()

    def guard():
        raise RuntimeError("running package changed")

    with pytest.raises(RuntimeError, match="running package"):
        delivery.GuardedClient(client, guard).post("models", {"name": "new"})
    assert not client.posts


@pytest.fixture
def state(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    primary = workspace / "outputs/camera-platform"
    primary.mkdir(parents=True)
    monkeypatch.setattr(delivery, "WORKSPACE", workspace)
    with sqlite3.connect(primary / "registry.sqlite") as connection:
        connection.executescript(
            "CREATE TABLE records(id TEXT PRIMARY KEY, data TEXT);"
            "CREATE TABLE jobs(id TEXT PRIMARY KEY, status TEXT);"
            "CREATE TABLE audit(seq INTEGER PRIMARY KEY, event TEXT);"
        )
        connection.execute("INSERT INTO records VALUES('source', 'original')")
        connection.execute("INSERT INTO jobs VALUES('job', 'succeeded')")
        connection.execute("INSERT INTO audit VALUES(1, 'original')")
    return primary


def test_snapshot_accepts_append_but_refuses_original_mutation(state):
    before = delivery.snapshot(state, require_idle=True)
    with sqlite3.connect(state / "registry.sqlite") as connection:
        connection.execute("INSERT INTO records VALUES('new', 'new')")
    delivery.preserved(before, delivery.snapshot(state))
    with sqlite3.connect(state / "registry.sqlite") as connection:
        connection.execute("UPDATE records SET data='changed' WHERE id='source'")
    with pytest.raises(RuntimeError, match="Original records"):
        delivery.preserved(before, delivery.snapshot(state))


def test_first_snapshot_refuses_live_job(state):
    with sqlite3.connect(state / "registry.sqlite") as connection:
        connection.execute("UPDATE jobs SET status='running'")
    with pytest.raises(RuntimeError, match="Finish existing jobs"):
        delivery.snapshot(state, require_idle=True)


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"records": {}, "jobs": {}, "audit": {}},
        {"records": {"r": "bad"}, "jobs": {}, "audit": {}},
    ],
)
def test_empty_or_malformed_baseline_never_proves_preservation(invalid):
    with pytest.raises(RuntimeError):
        delivery.preserved(invalid, invalid)


def test_primary_path_requires_canonical_exact_workspace(state, tmp_path):
    delivery.exact_primary(state, delivery.WORKSPACE)
    alias = tmp_path / "alias"
    alias.symlink_to(state)
    for wrong in [alias, state.parent, Path("relative"), state / "child"]:
        with pytest.raises(RuntimeError):
            delivery.exact_primary(wrong, delivery.WORKSPACE)


def test_baseline_requires_exact_file_hash_and_state(state, tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "schema": "camera_prescription_release_primary_baseline_v1",
                "health": {"state_dir": str(state)},
                "rows": delivery.snapshot(state),
            }
        )
    )
    assert delivery.load_baseline(baseline, delivery.sha(baseline), state)["rows"]
    with pytest.raises(RuntimeError, match="hash differs"):
        delivery.load_baseline(baseline, "0" * 64, state)


def test_bare_upgrade_baseline_requires_exact_existing_rows_and_file_hash(state, tmp_path):
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(delivery.snapshot(state)))
    assert delivery.load_baseline(path, delivery.sha(path), state)["rows"] == delivery.snapshot(
        state
    )
    changed = json.loads(path.read_text())
    changed["records"]["source"] = "a" * 64
    path.write_text(json.dumps(changed))
    with pytest.raises(RuntimeError, match="Original records"):
        delivery.load_baseline(path, delivery.sha(path), state)


def test_output_cannot_alias_protected_state_runtime_or_baseline(state, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    baseline = tmp_path / "baseline.json"
    baseline.write_text("original")
    alias = tmp_path / "state-alias"
    alias.symlink_to(state)
    for output in [
        state / "new.json",
        runtime / "new.json",
        baseline,
        alias / "new.json",
        tmp_path / "foo/../workspace/outputs/camera-platform/new.json",
    ]:
        with pytest.raises(RuntimeError):
            delivery.safe_output(output, state, runtime, baseline)
    assert (
        delivery.safe_output(tmp_path / "new.json", state, runtime, baseline)
        == tmp_path / "new.json"
    )
    assert baseline.read_text() == "original"


@pytest.mark.parametrize(
    "fault", ["pin", "version", "state", "worker", "source", "running", "client"]
)
def test_preflight_refuses_wrong_binding_before_any_post(state, tmp_path, fault):
    runtime = tmp_path / "runtime"
    args = SimpleNamespace(
        expected_state=state,
        expected_runtime_root=runtime,
        expected_manifest_sha256="a" * 64,
        expected_version="0.14.0",
        source_assembly_id="source",
        base_url="http://127.0.0.1:8770",
    )
    binding = {
        "runtime_root": str(runtime),
        "manifest_sha256": "a" * 64,
        "manifest": {"environment": {"platform_version": "0.14.0"}},
    }
    responses = {
        name: {"schema": "camera_platform_v1", "state_dir": str(state), "version": "0.14.0"}
        for name in ["health", "session", "bootstrap"]
    }
    responses["health"].update(workspace=str(delivery.WORKSPACE), status="ok", worker="running")
    if fault == "pin":
        binding["manifest_sha256"] = "b" * 64
    elif fault == "version":
        responses["session"]["version"] = "0.13.0"
    elif fault == "state":
        responses["bootstrap"]["state_dir"] = str(tmp_path)
    elif fault == "worker":
        responses["health"]["worker"] = "stopped"
    elif fault == "source":
        args.source_assembly_id = "unrelated"

    def running(*args):
        if fault == "running":
            raise RuntimeError("running/static mismatch")

    helper = SimpleNamespace(
        __version__="0.14.0",
        installed_binding=lambda root: {
            "manifest_sha256": "b" * 64 if fault == "client" else "a" * 64
        },
    )
    launcher = SimpleNamespace(
        load_installed_runtime=lambda state: binding, verify_running_package=running
    )
    client = SimpleNamespace(
        get=lambda path: responses[path], post=lambda *args: pytest.fail("POST")
    )
    with pytest.raises(RuntimeError):
        delivery.preflight(client, args, helper, launcher, {"rows": delivery.snapshot(state)})


def test_real_chart_hash_matches_product_fixture(tmp_path):
    import numpy as np

    from camera_platform import imaging
    from camera_platform.demo import seed
    from camera_platform.service import PlatformService

    service = PlatformService(tmp_path / "fixture")
    seed(service)
    assembly = service.registry.list("assemblies")[0]
    request = imaging.fixture_template(service, assembly["id"])
    image = imaging.create_fixture(service, request)
    assert delivery.chart_sha256(request, image["wavelength_nm"], np) == next(
        artifact["sha256"]
        for artifact in image["artifacts"]
        if artifact["name"] == "spectral-image.npy"
    )


def test_bundle_semantics_survive_zip_timestamps_but_reject_duplicate_paths():
    from camera_platform.registry import digest

    model = {"id": "model", "parents": []}
    run = {"id": "run", "model_snapshots": [model], "artifacts": []}
    manifest = {"run": run, "model_lineage": [model], "factory_jobs": []}
    manifest["package_hash"] = digest(manifest)
    contents = []
    for year in [2020, 2021]:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                zipfile.ZipInfo("manifest.json", (year, 1, 1, 0, 0, 0)), json.dumps(manifest)
            )
        contents.append(buffer.getvalue())
    client = SimpleNamespace(get=lambda path: model)
    helper = SimpleNamespace(fetch=lambda *args: contents.pop(0), digest=digest)
    first = delivery.bundle_evidence(client, "runs", run, [], None, helper)
    second = delivery.bundle_evidence(client, "runs", run, [], None, helper)
    assert first.pop("download_sha256") != second.pop("download_sha256")
    assert first == second
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        with pytest.warns(UserWarning):
            archive.writestr("manifest.json", json.dumps(manifest))
    helper.fetch = lambda *args: buffer.getvalue()
    with pytest.raises(RuntimeError, match="Duplicate ZIP"):
        delivery.bundle_evidence(client, "runs", run, [], None, helper)


def test_actual_recipe_then_saved_recheck_uses_zero_posts(tmp_path):
    from fastapi.testclient import TestClient

    from camera_platform.api import create_app

    sys.path.insert(0, str(TOOLS))
    try:
        import verify_camera_prescription_optics as helper
    finally:
        sys.path.remove(str(TOOLS))
    app = create_app(tmp_path / "recipe", start_worker=False)
    service = app.state.service
    with TestClient(app) as http:
        bootstrap = http.get("/api/v1/bootstrap").json()
        http.headers["X-Platform-Token"] = bootstrap["token"]

        class RecipeClient:
            def __init__(self):
                self.http = http
                self.posts = 0

            def get(self, path):
                response = http.get("/api/v1/" + path)
                response.raise_for_status()
                return response.json()

            def post(self, path, payload):
                self.posts += 1
                response = http.post("/api/v1/" + path, json=payload)
                response.raise_for_status()
                record = response.json()
                if path in {"factory/jobs", "runs", "replays"}:
                    claimed = service.registry.claim()
                    assert claimed["id"] == record["id"]
                    service.registry.finish(claimed["id"], result=service.execute_run(claimed))
                return record

        client = RecipeClient()
        journal = delivery.Journal(tmp_path / "recipe.json", {"test": "ephemeral-service"})
        journal.pin("environment", helper.environment_fingerprint())
        source = bootstrap["assemblies"][0]["id"]
        # A primary delivery baseline contains previous completed work; establish
        # one real preexisting run so every protected table is nonempty.
        service.submit_run(
            {
                "assembly_id": source,
                "test_id": bootstrap["tests"][0]["id"],
                "plugin_id": "response_db",
                "parameters": {"seed": 41},
            }
        )
        prior = service.registry.claim()
        service.registry.finish(prior["id"], result=service.execute_run(prior))
        before = delivery.snapshot(service.registry.directory, require_idle=True)
        try:
            delivery.recipe(client, journal, helper, source, 30)
            delivery.preserved(before, delivery.snapshot(service.registry.directory))
            assert client.posts == 11
            journal.data["first_verified_at"] = delivery.stamp()
            journal.save()
            saved = copy.deepcopy(journal.data["records"])
            rows = delivery.snapshot(service.registry.directory)
            journal = reopen(journal, read_only=True)
            delivery.recipe(client, journal, helper, source, 30)
            assert journal.data["latest_invocation"]["post_count"] == 0
            assert client.posts == 11 and journal.data["records"] == saved
            assert delivery.snapshot(service.registry.directory) == rows
            assert journal.data["physical_validation"] == "not_established"
        finally:
            journal.lock.close()
