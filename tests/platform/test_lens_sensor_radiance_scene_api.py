"""HTTP/SDK import of object radiance, separate from image-plane irradiance."""

from __future__ import annotations

import hashlib
import io
import json

import numpy as np
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient


def test_object_radiance_import_is_source_bound_and_downloadable(tmp_path):
    app = create_app(tmp_path / "state", tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        client = object.__new__(PlatformClient)
        client.http = transport
        metadata = client.lens_sensor_radiance_scene_template()
        metadata.update(
            name="independent object radiance fixture",
            source_uri="fixture://object-radiance-01",
            license="test-authored synthetic fixture",
            method_and_version="independent-npy-fixture:1",
        )
        grid = np.linspace(0.1, 0.9, 32 * 32, dtype=np.float32).reshape(32, 32, 1)
        radiance = np.ascontiguousarray(grid * np.array([1.0, 0.8, 0.5]))
        buffer = io.BytesIO()
        np.save(buffer, radiance, allow_pickle=False)
        path = tmp_path / "object-radiance.npy"
        content = buffer.getvalue()
        path.write_bytes(content)

        created = client.import_lens_sensor_radiance_scene(metadata, path)
        assert created["reference_plane"] == "object_directional_radiance_before_lens"
        assert created["unit"] == "W/m^2/sr/nm"
        assert created["shape"] == [32, 32, 3]
        assert created["source_sha256"] == hashlib.sha256(content).hexdigest()
        assert created["physical_validation"] == "not_established"
        assert client.lens_sensor_radiance_scene(created["id"])["content_hash"] == created[
            "content_hash"
        ]
        assert [row["id"] for row in client.list_lens_sensor_radiance_scenes()] == [
            created["id"]
        ]
        assert client.import_lens_sensor_radiance_scene(metadata, path)["id"] == created["id"]
        downloaded = client.download_artifact(created["artifacts"][0], tmp_path / "copy.npy")
        assert downloaded.read_bytes() == content

        wrong_plane = dict(metadata, reference_plane="sensor_ideal_image_before_psf_ri")
        response = transport.post(
            "/api/v1/lens-sensor-radiance-scenes/import",
            data={"metadata": json.dumps(wrong_plane)},
            files={"image": (path.name, content, "application/octet-stream")},
        )
        assert response.status_code == 422
        assert len(client.list_lens_sensor_radiance_scenes()) == 1
