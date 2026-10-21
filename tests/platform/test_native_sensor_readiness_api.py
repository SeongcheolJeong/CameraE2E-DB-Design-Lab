"""The API binds a native sensor data request to one verified Factory Lens."""

from __future__ import annotations

from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient


def test_sdk_request_uses_registered_lens_and_server_workspace(tmp_path, monkeypatch):
    from camera_platform import native_response_publication as publication
    from camera_platform import native_sensor_readiness as readiness

    app = create_app(tmp_path / "state", tmp_path, start_worker=False, seed_demo=False)
    psf = {"sample_pitch_um": 3.0}
    directional_response = {"schema": "test-directional"}
    calls = {}

    def qualified_lens(service, identity):
        assert service is app.state.service
        calls["lens_identity"] = identity
        return {
            "id": "model-lens-a",
            "content_hash": "a" * 64,
            "payload": {"psf": psf, "directional_response": directional_response},
        }

    def request(identifier, **kwargs):
        calls["sensor_identifier"] = identifier
        calls["request_kwargs"] = kwargs
        return {
            "schema": "camera_native_sensor_data_request_v1",
            "status": "source_data_required",
            "product_ranking_allowed": False,
        }

    monkeypatch.setattr(publication, "_qualified_lens", qualified_lens)
    monkeypatch.setattr(readiness, "native_sensor_data_request", request)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        result = client.native_sensor_data_request(
            lens_model_id="model-lens-a",
            sensor_identifier="DEF 1803/805",
            run_id="run & 1",
        )

    assert calls == {
        "lens_identity": "model-lens-a",
        "sensor_identifier": "DEF 1803/805",
        "request_kwargs": {
            "psf": psf,
            "directional_response": directional_response,
            "repository_root": app.state.service.workspace,
            "run_id": "run & 1",
        },
    }
    assert result["registered_lens_binding"] == {
        "model_id": "model-lens-a",
        "model_content_hash": "a" * 64,
    }
    assert result["product_ranking_allowed"] is False


def test_unknown_lens_is_not_accepted_as_an_unbound_payload(tmp_path):
    app = create_app(tmp_path / "state", tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        response = transport.get(
            "/api/v1/native-sensor-data-requests",
            params={
                "lens_model_id": "missing-lens",
                "sensor_identifier": "DEF-1803-805",
            },
        )
    assert response.status_code == 404
    assert app.state.service.registry.list("models") == []
