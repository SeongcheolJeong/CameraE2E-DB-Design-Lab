"""Isolated HTTP/owner-binding regression tests for the file-first E-SFR Lab."""

import json
from io import BytesIO

import numpy as np
import pytest
from camera_platform.esfr_lab import CAPTURES, ROOT
from fastapi.testclient import TestClient
from PIL import Image

from camera_platform.api import create_app


@pytest.fixture
def lab(tmp_path):
    app = create_app(
        state_dir=tmp_path / "state", workspace=tmp_path, start_worker=False, seed_demo=False
    )
    with TestClient(app) as client:
        token = client.get("/api/v1/session").json()["token"]
        yield client, app.state.service, {"X-Platform-Token": token}


def acquisition(
    client, headers, *, role="development", serial=0, session=None, parent=None, same_pixels=False
):
    metadata = client.get(ROOT + "/template").json()["capture_metadata"]
    # These bytes are generated below; never label API fixtures as physical
    # measurements or inherit plausible settings from the measured upload form.
    metadata["raw_format"] = dict(packing="unpacked16", width=64, height=64, stride_bytes=128,
        header_bytes=0, byte_order="little", bit_depth=12, bit_shift=0, cfa="RGGB",
        black_level_dn=64, white_level_dn=4095)
    metadata["geometry"] = dict(full_sensor_shape=[64, 64], crop_origin=[0, 0])
    metadata["conditions"] = {"light": "synthetic-fixture", "exposure_s": 0.01}
    metadata["isp"] = dict(transfer="srgb", coordinate_mapping="same_native_raster")
    metadata.update(
        camera_id="camera-api-test",
        acquisition_id=f"acq-{serial}",
        session_id=session or role + "-session",
        role=role,
        source_kind="synthetic",
    )
    if parent is not None:
        metadata["parent_acquisition_ids"] = [parent]
    yy, xx = np.indices((64, 64))
    raw = np.where(xx + yy / 10 > 32, 3000, 1000).astype("<u2")
    if not same_pixels:
        raw[0, 0] += serial * 32
    rgb = np.repeat(np.uint8(raw / 16)[..., None], 3, axis=2)
    image = BytesIO()
    Image.fromarray(rgb).save(image, format="PNG")
    response = client.post(
        ROOT + "/captures",
        headers=headers,
        data={"metadata": json.dumps(metadata)},
        files={"raw": ("fixture.raw", raw.tobytes()), "isp": ("fixture.png", image.getvalue())},
    )
    assert response.status_code == 201, response.text
    return response.json(), raw.tobytes()


def study_payload(dev, val):
    return {
        "name": "API boundary check",
        "development_ids": [dev["id"]],
        "validation_ids": [val["id"]],
        "fit": False,
        "rois": [{"id": "edge-1", "kind": "edge", "box": [4, 4, 56, 56]}],
    }


def test_template_static_assets_and_empty_lists(lab):
    client, _, _ = lab
    value = client.get(ROOT + "/template").json()
    assert value["capture_metadata"]["source_kind"] == "measured"
    assert value["capabilities"]["iso_algorithm_conformance"] == "not_established"
    for suffix in ("/captures", "/studies"):
        assert client.get(ROOT + suffix).json() == []
    assert client.get(ROOT + "/active").json() is None
    for extension in ("html", "js", "css"):
        assert client.get("/camera-validation-esfr." + extension).status_code == 200


def test_upload_original_owner_binding_preview_and_explicit_detection(lab):
    client, service, headers = lab
    row, original = acquisition(client, headers)
    saved = service.registry.get(CAPTURES, row["id"])
    bound = service.registry.artifact_binding(
        saved["files"]["raw"]["sha256"], owner_kind=CAPTURES, owner_id=row["id"]
    )
    assert bound["path"].read_bytes() == original
    assert saved["metadata"]["source_kind"] == "synthetic"
    assert saved["physical_identity_verified"] is False
    preview = client.get(row["preview_url"])
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"] == "image/png"
    with Image.open(BytesIO(preview.content)) as im:
        assert im.size == (64, 64)
    rois = [{"id": "edge-1", "kind": "edge", "box": [4, 4, 56, 56]}]
    detected = client.post(
        ROOT + "/detect",
        headers=headers,
        json={"capture_id": row["id"], "chart_profile": {"rois": rois}},
    )
    assert detected.status_code == 200, detected.text
    result = detected.json()
    assert result["rois"][0]["box"] == rois[0]["box"]
    assert result["requires_roi_review"] is True
    assert result["registration"]["transform_applied_to_measurements"] is False
    assert client.get(ROOT + "/captures/" + row["id"]).json()["id"] == row["id"]
    assert [x["id"] for x in client.get(ROOT + "/captures").json()] == [row["id"]]


def test_mutations_require_session_token_and_same_origin(lab):
    client, _, headers = lab
    assert client.post(ROOT + "/studies", json={}).status_code == 403
    assert (
        client.post(
            ROOT + "/studies", json={}, headers={**headers, "Origin": "https://other.example"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            ROOT + "/studies", content="{}", headers={**headers, "Content-Type": "text/plain"}
        ).status_code
        == 415
    )


def test_valid_submission_pins_capture_roles_source_and_lists_run(lab):
    client, service, headers = lab
    dev, _ = acquisition(client, headers, serial=1)
    val, _ = acquisition(client, headers, serial=2, role="validation")
    response = client.post(ROOT + "/studies", headers=headers, json=study_payload(dev, val))
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["status"] == "queued"
    assert job["development_ids"] == [dev["id"]]
    assert job["validation_ids"] == [val["id"]]
    assert len(job["capture_snapshots"]) == 2
    assert job["source_code"] == service.loaded_environment
    assert client.get(ROOT + "/studies/" + job["id"]).json()["id"] == job["id"]
    assert any(x["id"] == job["id"] for x in client.get(ROOT + "/studies").json())
    assert client.get(ROOT + "/studies/" + job["id"] + "/files/report.html").status_code == 422


def test_role_cannot_be_relabelled(lab):
    client, _, headers = lab
    dev, _ = acquisition(client, headers, serial=1)
    val, _ = acquisition(client, headers, serial=2, role="validation")
    response = client.post(ROOT + "/studies", headers=headers, json=study_payload(val, dev))
    assert response.status_code == 422
    assert "roles are immutable" in response.json()["error"]


@pytest.mark.parametrize("overlap", ["session", "parent", "pixels"])
def test_independent_validation_rejects_shared_lineage(lab, overlap):
    client, _, headers = lab
    dev, _ = acquisition(client, headers, serial=0)
    opts = (
        {"session": "development-session"}
        if overlap == "session"
        else ({"parent": "acq-0"} if overlap == "parent" else {"same_pixels": True})
    )
    val, _ = acquisition(client, headers, serial=1, role="validation", **opts)
    response = client.post(ROOT + "/studies", headers=headers, json=study_payload(dev, val))
    assert response.status_code == 422, response.text
    assert "overlaps" in response.json()["error"]


def test_artifact_owner_cannot_be_substituted(lab):
    client, service, headers = lab
    first, _ = acquisition(client, headers, serial=1)
    second, _ = acquisition(client, headers, serial=2)
    with pytest.raises(KeyError, match="exact owner"):
        service.registry.artifact_binding(
            first["files"]["raw"]["sha256"], owner_kind=CAPTURES, owner_id=second["id"]
        )


def test_frozen_chart_assets_and_readonly_reference_provenance(lab):
    import hashlib

    client, _, _ = lab
    package = client.get(ROOT + "/chart-template").json()
    assert package["counts"]["slanted_stars"] == 13
    assert package["counts"]["radial_star_edges"] == 104
    assert package["counts"]["side_square_edges"] == 8
    assert package["counts"]["wedge_groups"] == 16
    assert package["photometry"]["screenshot_used_as_radiance"] is False
    assert package["qualification"]["physical_camera_validation"] == "not_established"
    for name, expected in package["asset_sha256"].items():
        response = client.get(ROOT + "/chart-assets/" + name)
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest() == expected
    assert client.get(ROOT + "/chart-assets/not-in-package.svg").status_code == 422
    profile = client.get(ROOT + "/template").json()["chart_profile"]
    assert profile["chart_geometry"] == package
    assert profile["rois"] == []  # Real photographs cannot inherit synthetic pixel coordinates.
