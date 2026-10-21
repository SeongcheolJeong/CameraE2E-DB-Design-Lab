"""HTTP boundary tests for the shared local engineering service."""

import hashlib
import io
import json
import zipfile
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.contracts import AssemblyCreate
from camera_platform.contracts import TestCreate as SpecificationContract
from camera_platform.sdk import PlatformClient


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / "api", start_worker=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/bootstrap")
        assert response.status_code == 200
        bootstrap = response.json()
        headers = {"x-platform-token": bootstrap["token"]}
        yield app, client, headers, bootstrap


def asset_payload():
    return {
        "name": "API lens asset",
        "kind": "lens",
        "source_kind": "estimated",
        "source_uri": "fixture://api-lens",
        "license": "MIT",
        "method_and_version": "api-test-fixture:1",
    }


def completed_run(api):
    app, client, headers, bootstrap = api
    response = client.post(
        "/api/v1/runs",
        json={
            "assembly_id": bootstrap["assemblies"][0]["id"],
            "test_id": bootstrap["tests"][0]["id"],
            "plugin_id": "response_db",
            "parameters": {"seed": 42, "frames": 2, "output_size": [8, 8]},
        },
        headers=headers,
    )
    assert response.status_code == 202, response.text
    queued = response.json()
    service = app.state.service
    claimed = service.registry.claim()
    assert claimed["id"] == queued["id"]
    output = service.execute_run(claimed)
    finished = service.registry.finish(claimed["id"], result=output)
    assert finished["status"] == "succeeded"
    return finished


def test_common_job_status_preserves_ordinary_sdk_wait_detail(api):
    _, http, _, _ = api
    run = completed_run(api)
    sdk = object.__new__(PlatformClient)
    sdk.http = http

    lifecycle = sdk.job_status(run["id"], expected_kind="simulation_run")
    assert lifecycle["schema"] == "camera_job_status_v1"
    assert lifecycle["job_kind"] == "simulation_run"
    assert lifecycle["status"] == "succeeded"
    assert "result" not in lifecycle and "artifacts" not in lifecycle

    completed = sdk.wait(run["id"], timeout=1, poll_interval=0)
    assert completed["result"] == run["result"]
    assert completed["artifacts"] == run["artifacts"]


@pytest.mark.parametrize("host", ["127.0.0.1:8768", "localhost:8768", "testserver"])
def test_loopback_hosts_and_exact_same_origin_are_accepted(api, host):
    _, client, _, _ = api
    response = client.get("/api/v1/health", headers={"host": host, "origin": f"http://{host}"})
    assert response.status_code == 200
    assert response.json()["worker"] == "external_or_stopped"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "headers",
    [
        {"host": "remote.example"},
        {"origin": "https://remote.example"},
        {"origin": "null"},
        {"host": "localhost:8768", "origin": "http://localhost:9999"},
    ],
)
def test_untrusted_host_or_origin_cannot_read_bootstrap_token(api, headers):
    _, client, _, _ = api
    response = client.get("/api/v1/bootstrap", headers=headers)
    assert response.status_code == 403
    assert "token" not in response.json()


@pytest.mark.parametrize("token", [None, "stale-token"])
def test_mutations_require_current_platform_token(api, token):
    _, client, _, bootstrap = api
    headers = {} if token is None else {"x-platform-token": token}
    response = client.post("/api/v1/assets", json=asset_payload(), headers=headers)
    assert response.status_code == 403
    assert len(client.get("/api/v1/assets").json()) == len(bootstrap["assets"])


def test_valid_token_does_not_override_cross_origin_rejection(api):
    _, client, headers, _ = api
    response = client.post(
        "/api/v1/assets",
        json=asset_payload(),
        headers={**headers, "origin": "https://remote.example"},
    )
    assert response.status_code == 403


def test_authorized_mutation_still_requires_json_content_type(api):
    _, client, headers, _ = api
    response = client.post(
        "/api/v1/assets",
        content=json.dumps(asset_payload()),
        headers={**headers, "content-type": "text/plain"},
    )
    assert response.status_code == 415


def test_unknown_contract_fields_return_422_without_creating_asset(api):
    _, client, headers, bootstrap = api
    response = client.post(
        "/api/v1/assets", json={**asset_payload(), "approval_status": "approved"}, headers=headers
    )
    assert response.status_code == 422, response.text
    assert len(client.get("/api/v1/assets").json()) == len(bootstrap["assets"])


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_nested_json_returns_422_instead_of_server_error(api, number):
    _, client, headers, bootstrap = api
    payload = {**asset_payload(), "properties": {"invalid_measurement": number}}
    response = client.post(
        "/api/v1/assets",
        content=json.dumps(payload),
        headers={**headers, "content-type": "application/json"},
    )
    assert response.status_code == 422, response.text
    assert len(client.get("/api/v1/assets").json()) == len(bootstrap["assets"])


def test_invalid_json_syntax_returns_422(api):
    _, client, headers, _ = api
    response = client.post(
        "/api/v1/assets",
        content=b"{invalid",
        headers={**headers, "content-type": "application/json"},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("route", ["/api/v1/assets", "/api/v1/measurements/import"])
def test_oversized_upload_is_rejected_before_route_processing(api, route):
    _, client, headers, bootstrap = api
    response = client.post(
        route,
        content=b'{"document_text":"' + b"x" * 2_097_152 + b'"}',
        headers={**headers, "content-type": "application/json"},
    )
    assert response.status_code == 413
    assert len(client.get("/api/v1/assets").json()) == len(bootstrap["assets"])
    assert client.get("/api/v1/measurements").json() == []


def test_chunked_upload_without_content_length_obeys_same_limit(api):
    _, client, headers, _ = api

    def chunks():
        yield b'{"document_text":"'
        for _ in range(33):
            yield b"x" * 65_536
        yield b'"}'

    response = client.post(
        "/api/v1/measurements/import",
        content=chunks(),
        headers={**headers, "content-type": "application/json", "transfer-encoding": "chunked"},
    )
    assert response.status_code == 413
    assert client.get("/api/v1/measurements").json() == []


def test_measurement_import_preserves_exact_utf8_source_bytes(api):
    _, client, headers, _ = api
    payload = client.get("/api/v1/templates/measurement").json()
    payload["name"] = "원본 공백과 한글 보존"
    original_text = json.dumps(payload, ensure_ascii=False, indent=3) + "\n\n"
    original_bytes = original_text.encode("utf-8")
    response = client.post(
        "/api/v1/measurements/import", json={"document_text": original_text}, headers=headers
    )
    assert response.status_code == 201, response.text
    measurement = response.json()
    artifact = measurement["original_artifact"]
    assert artifact["sha256"] == hashlib.sha256(original_bytes).hexdigest()
    assert measurement["original_encoding"] == "verbatim_utf8_json"
    downloaded = client.get(artifact["url"])
    assert downloaded.status_code == 200
    assert downloaded.content == original_bytes
    assert "attachment" in downloaded.headers["content-disposition"]


@pytest.mark.parametrize("import_route", [False, True])
def test_invalid_measured_contract_returns_422_and_stores_nothing(api, import_route):
    _, client, headers, _ = api
    payload = client.get("/api/v1/templates/measurement").json()
    payload.update(source_kind="measured", physical_instance="camera-42", instrument={})
    route = "/api/v1/measurements/import" if import_route else "/api/v1/measurements"
    body = {"document_text": json.dumps(payload)} if import_route else payload
    response = client.post(route, json=body, headers=headers)
    assert response.status_code == 422, response.text
    assert client.get("/api/v1/measurements").json() == []


def test_invalid_measurement_import_json_is_a_client_error(api):
    _, client, headers, _ = api
    response = client.post(
        "/api/v1/measurements/import", json={"document_text": "{not-json}"}, headers=headers
    )
    assert response.status_code == 422
    assert client.get("/api/v1/measurements").json() == []


def test_asset_model_assembly_and_test_revisions_use_public_routes(api):
    _, client, headers, bootstrap = api
    asset = client.post("/api/v1/assets", json=asset_payload(), headers=headers)
    assert asset.status_code == 201, asset.text
    assert asset.json()["approval_status"] == "draft"
    model_payload = client.get("/api/v1/templates/model").json()
    model_payload["name"] = "API-imported lens response"
    model = client.post("/api/v1/models", json=model_payload, headers=headers)
    assert model.status_code == 201, model.text
    assembly = bootstrap["assemblies"][0]
    models = {m["id"]: m for m in bootstrap["models"]}
    assembly_payload = {
        key: deepcopy(value)
        for key, value in assembly.items()
        if key in AssemblyCreate.model_fields
    }
    assembly_payload["name"] = "API composed camera"
    assembly_payload["model_ids"] = [
        model.json()["id"] if models[identity]["kind"] == "lens" else identity
        for identity in assembly["model_ids"]
    ]
    created = client.post("/api/v1/assemblies", json=assembly_payload, headers=headers)
    assert created.status_code == 201, created.text
    checked = client.post(
        "/api/v1/assemblies/check", json={"assembly_id": created.json()["id"]}, headers=headers
    )
    assert checked.status_code == 200
    assert checked.json()["status"] == "PASS"
    test = bootstrap["tests"][0]
    test_payload = {
        key: deepcopy(value)
        for key, value in test.items()
        if key in SpecificationContract.model_fields
    }
    test_payload.update(name="API fixed-condition test", revision="2")
    created_test = client.post("/api/v1/tests", json=test_payload, headers=headers)
    assert created_test.status_code == 201, created_test.text
    assert created_test.json()["id"] != test["id"]
    assert client.get(f"/api/v1/tests/{test['id']}").json() == test


def test_completed_run_api_exports_verifiable_artifacts_and_unknown_validation(api):
    app, client, headers, _ = api
    run = completed_run(api)
    fetched = client.get(f"/api/v1/runs/{run['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "succeeded"
    exported = client.get(f"/api/v1/runs/{run['id']}/export")
    assert exported.status_code == 200
    assert exported.json()["run"]["id"] == run["id"]
    artifact = next(a for a in run["artifacts"] if a["name"] == "raw_frames.npy")
    downloaded = client.get(artifact["url"])
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == artifact["sha256"]
    run_bundle = client.get(f"/api/v1/runs/{run['id']}/bundle")
    assert run_bundle.status_code == 200, run_bundle.text
    assert run_bundle.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(run_bundle.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["run"]["id"] == run["id"]
        for item in run["artifacts"]:
            contents = archive.read(f"artifacts/{item['sha256']}/{item['name']}")
            assert hashlib.sha256(contents).hexdigest() == item["sha256"]

    payload = client.get("/api/v1/templates/measurement").json()
    prediction = run["result"]["prediction"]
    payload.update(
        name="HTTP imported independent sample",
        source_kind="measured",
        physical_instance="camera-api-42",
        instrument={"id": "instrument-api-7", "calibration_revision": "2026-09-13"},
        dataset_ids=["independent:api-42"],
        assembly_id=run["assembly_id"],
        test_id=run["test_id"],
        values=prediction["values"],
        axes=prediction["axes"],
        conditions=prediction["conditions"],
        included_effects=prediction["included_effects"],
        uncertainty={"kind": "absolute_bound", "value": 0.01, "unit": "1"},
    )
    imported = client.post("/api/v1/measurements", json=payload, headers=headers)
    assert imported.status_code == 201, imported.text
    compared = client.post(
        "/api/v1/validations",
        json={
            "run_id": run["id"],
            "measurement_id": imported.json()["id"],
            "test_id": run["test_id"],
        },
        headers=headers,
    )
    assert compared.status_code == 201, compared.text
    assert compared.json()["status"] == "INCONCLUSIVE", compared.text
    assert not compared.json()["physical_validation_pass"]

    release = client.post(
        "/api/v1/releases",
        json={
            "name": "API evidence package",
            "run_ids": [run["id"]],
            "validation_ids": [compared.json()["id"]],
        },
        headers=headers,
    )
    assert release.status_code == 201, release.text
    assert release.json()["status"] == "blocked"
    release_bundle = client.get(f"/api/v1/releases/{release.json()['id']}/bundle")
    assert release_bundle.status_code == 200, release_bundle.text
    original_artifact = imported.json()["original_artifact"]
    with zipfile.ZipFile(io.BytesIO(release_bundle.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["release"]["approval_status"] == "unapproved"
        assert manifest["measurements"][0]["id"] == imported.json()["id"]
        original_bytes = archive.read(
            f"artifacts/{original_artifact['sha256']}/{original_artifact['name']}"
        )
        assert hashlib.sha256(original_bytes).hexdigest() == original_artifact["sha256"]

    app.state.service.registry.blob_path(artifact["sha256"]).write_bytes(b"damaged artifact")
    assert client.get(artifact["url"]).status_code == 409
    assert client.get(f"/api/v1/runs/{run['id']}/export").status_code == 409
    assert client.get(f"/api/v1/runs/{run['id']}/bundle").status_code == 409


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/unknown",
        "/api/v1/assets/nonexistent",
        "/api/v1/templates/unknown",
        "/.env",
        "/registry.sqlite",
    ],
)
def test_unknown_paths_and_internal_filenames_are_not_exposed(api, path):
    _, client, _, _ = api
    assert client.get(path).status_code == 404


def test_unregistered_blob_is_not_publicly_downloadable(api):
    app, client, _, _ = api
    orphan = app.state.service.registry.store_blob(
        b"unreferenced", "private.bin", "application/octet-stream"
    )
    assert client.get(orphan["url"]).status_code == 404


def test_openapi_exposes_the_versioned_contracts(api):
    _, client, _, _ = api
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "/api/v1/models" in schema["paths"]
    assert "/api/v1/measurements/import" in schema["paths"]
    assert "/api/v1/validations" in schema["paths"]
    assert "MeasurementCreate" in schema["components"]["schemas"]
    assert client.get("/docs").status_code == 200
