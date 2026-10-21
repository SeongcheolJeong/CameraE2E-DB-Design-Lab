"""Authoring must preserve unknown optics and display the actual spectral output."""

import copy
import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from camera_platform.api import create_app
from camera_platform.scene_render import check_auxiliary_outputs, store_previews
from camera_platform.service import PlatformService


def test_readonly_templates_keep_unknown_optics_and_empty_registry_usable(tmp_path):
    app = create_app(tmp_path / "state", start_worker=False)
    with TestClient(app) as client:
        boot = client.get("/api/v1/workbench").json()
        client.headers["X-Platform-Token"] = boot["token"]
        result = client.get("/api/v1/scene_authoring/templates")
        assert result.status_code == 200, result.text
        templates = result.json()
        assert set(templates["assets"]) == {"scene", "material", "geometry", "light"}
        assert client.get("/api/v1/workbench").json()["counts"] == boot["counts"]
        scene = client.post("/api/v1/scene_assets", json=templates["assets"]["scene"])
        assert scene.status_code == 201, scene.text
        result = client.get(
            "/api/v1/scene_authoring/render-template",
            params={
                "scene_id": scene.json()["id"],
                "assembly_id": boot["assemblies"][0]["id"],
            },
        )
        assert result.status_code == 200, result.text
        draft = result.json()
        assert draft["request"]["projection"]["focal_length_mm"] is None
        assert all(
            v is None for v in draft["request"]["projection"]["on_axis_transmittance"]["values"]
        )
        assert draft["issues"] and draft["physical_validation"] == "not_established"
        assert draft["scene_illumination_id"] == scene.json()["illumination_id"]
        # A draft with missing physical quantities must never enter the execution queue.
        before = client.get("/api/v1/workbench").json()["counts"]
        assert client.post("/api/v1/scene_renders", json=draft["request"]).status_code == 422
        assert client.get("/api/v1/workbench").json()["counts"] == before


@pytest.mark.parametrize("all_miss", [False, True])
def test_spectral_preview_uses_common_physical_scale_and_explicit_depth_mask(tmp_path, all_miss):
    service = PlatformService(tmp_path / "state")
    radiance = np.broadcast_to([0.5, 1.0], (4, 4, 2)).copy()
    depth = np.full((4, 4), 3.0)
    valid = np.ones((4, 4), dtype=bool)
    valid[0, 0] = False
    if all_miss:
        valid[:] = False
        radiance[:] = 0
    depth[~valid] = 0
    original = copy.deepcopy((radiance, depth, valid))
    display, artifacts = store_previews(service, radiance, depth, valid, [450.0, 650.0])
    assert len(artifacts) == 3
    bands = display["previews"][:2]
    pixels = [
        np.asarray(
            Image.open(io.BytesIO(service.registry.blob_path(p["artifact"]["sha256"]).read_bytes()))
        )
        for p in display["previews"]
    ]
    assert [b["display_max"] for b in bands] == [float(radiance.max())] * 2
    assert np.all(pixels[0] == (0 if all_miss else 128))
    assert np.all(pixels[1] == (0 if all_miss else 255))
    assert np.all(pixels[2][~valid] == 0)
    if not all_miss:
        assert np.any(pixels[2][valid] > 0)
    assert display["previews"][2]["display_min"] == (None if all_miss else 3.0)
    for a, b in zip(original, (radiance, depth, valid), strict=True):
        np.testing.assert_array_equal(a, b)


@pytest.mark.parametrize("fault", ["shape", "mask", "distance", "object", "error", "miss"])
def test_invalid_renderer_maps_cannot_be_displayed_as_evidence(fault):
    radiance = np.ones((4, 4, 2))
    depth, valid = np.ones((4, 4)), np.ones((4, 4), dtype=bool)
    objects, error = np.zeros((4, 4), dtype=np.int32), np.zeros_like(radiance)
    check_auxiliary_outputs(radiance, depth, valid, objects, error, 1)
    if fault == "shape":
        depth = depth[:2]
    if fault == "mask":
        valid = valid.astype(np.int32)
    if fault == "distance":
        depth[0, 0] = np.nan
    if fault == "object":
        objects[0, 0] = 1
    if fault == "error":
        error[0, 0, 0] = -1
    if fault == "miss":
        valid[0, 0] = False
    with pytest.raises(ValueError, match="auxiliary maps"):
        check_auxiliary_outputs(radiance, depth, valid, objects, error, 1)
