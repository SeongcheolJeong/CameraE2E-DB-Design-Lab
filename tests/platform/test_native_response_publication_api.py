"""The native response SDK sends bytes to the local publication endpoint."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient


def test_sdk_uploads_bundle_bytes_and_publication_fields(tmp_path, monkeypatch):
    from camera_platform import native_response_publication as publication

    app = create_app(tmp_path / "state", tmp_path, start_worker=False, seed_demo=False)
    uploaded = b"test-only archive transport"
    archive = tmp_path / "native-response.zip"
    archive.write_bytes(uploaded)
    called = {}

    def extract(source: Path, destination: Path) -> Path:
        assert source.read_bytes() == uploaded
        called["archive_name"] = source.name
        return destination

    def publish(service, bundle_dir: Path, **fields):
        assert service is app.state.service
        assert bundle_dir.name == "bundle"
        called["fields"] = fields
        return {"schema": "camera_native_response_publication_v1", "model_id": "model-test"}

    monkeypatch.setattr(publication, "extract_native_response_bundle", extract)
    monkeypatch.setattr(publication, "publish_native_response_bundle", publish)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        client = object.__new__(PlatformClient)
        client.http = transport
        result = client.publish_native_response_bundle(
            archive,
            expected_manifest_sha256="a" * 64,
            lens_model_id="model-lens",
            readout_model_id="model-readout",
            name="Native response research draft",
            revision="1",
            license="test-only source",
        )
        assert result["model_id"] == "model-test"
    assert called["archive_name"] == "bundle.zip"
    assert called["fields"] == {
        "expected_manifest_sha256": "a" * 64,
        "lens_model_id": "model-lens",
        "readout_model_id": "model-readout",
        "name": "Native response research draft",
        "revision": "1",
        "license": "test-only source",
    }


def test_invalid_zip_is_rejected_before_any_asset_is_written(tmp_path):
    app = create_app(tmp_path / "state", tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        response = transport.post(
            "/api/v1/native-response-bundles/import",
            data={
                "expected_manifest_sha256": "a" * 64,
                "lens_model_id": "model-lens",
                "readout_model_id": "model-readout",
                "name": "Invalid bundle",
                "revision": "1",
                "license": "test-only",
            },
            files={"bundle": ("invalid.zip", b"not a ZIP", "application/zip")},
        )
    assert response.status_code == 422
    assert app.state.service.registry.list("assets") == []
