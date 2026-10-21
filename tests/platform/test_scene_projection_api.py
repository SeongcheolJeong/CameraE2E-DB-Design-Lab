"""Public routes and SDK preserve projection identity and portable source evidence."""

import copy
import hashlib
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from test_scene_projection import (
    draft,
    projection_source,  # noqa: F401
    scene_workflow,  # noqa: F401
    target_camera,
)
from test_scene_render import finish

from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient


def client_for(service):
    return TestClient(create_app(
        service.registry.directory, start_worker=False, seed_demo=False,
    ))


def test_public_projection_sdk_publication_and_original_downloads(projection_source):  # noqa: F811
    service, assembly, source = projection_source
    target = target_camera(service, assembly)
    payload = draft(service, source, target)
    with client_for(service) as client:
        boot = client.get("/api/v1/workbench").json()
        assert client.post("/api/v1/scene_projections/preview", json=payload).status_code == 403
        client.headers["X-Platform-Token"] = boot["token"]
        sdk = PlatformClient.__new__(PlatformClient)
        sdk.http = client
        template = client.get("/api/v1/scene_authoring/projection-template", params={
            "source_render_id": source["id"], "assembly_id": target["id"],
        })
        assert template.status_code == 200, template.text
        assert all(v is None for v in template.json()["request"]["projection"]
                   ["on_axis_transmittance"]["values"])
        plan = sdk.preview_scene_projection(payload)
        assert plan["status"] == "ready"
        different_rays = copy.deepcopy(payload)
        different_rays["projection"]["focal_length_mm"] *= 2
        assert sdk.preview_scene_projection(different_rays)["status"] == "rerender_required"
        assert client.get("/api/v1/workbench").json()["counts"] == boot["counts"]
        changed = copy.deepcopy(payload)
        changed["name"] += " changed"
        stale = client.post("/api/v1/scene_projections", json={
            "draft": changed, "preview_sha256": plan["preview_sha256"],
        })
        assert stale.status_code == 409, stale.text
        queued = sdk.project_scene_radiance(plan)
        assert sdk.get("scene_projections") == [queued]
        assert sdk.get("runs") == []
        assert client.get("/api/v1/runs/" + queued["id"]).status_code == 404
        job = finish(service, queued)
        assert sdk.wait_scene_projection(job["id"], timeout=1) == job
        workbench = sdk.get("workbench")
        assert workbench["counts"]["scene_projections"] == 1
        assert workbench["counts"]["runs"] == 0
        assert sdk.get("bootstrap")["scene_projections"] == [job]
        image = sdk.publish_scene_projection(job["id"], "Projected source")
        counts = sdk.get("workbench")["counts"]
        assert sdk.publish_scene_projection(job["id"], "Projected source") == image
        assert sdk.get("workbench")["counts"] == counts
        assert image["projection_job_snapshot"] == job
        assert image["source_render_snapshot"] == source
        assert "render_job_snapshot" not in image
        bundle = client.get("/api/v1/scene_projections/" + job["id"] + "/bundle")
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as z:
            names = [n for n in z.namelist() if n.endswith("/spectral-image.npy")]
            assert len(names) == 2
            checksums = {hashlib.sha256(z.read(n)).hexdigest() for n in names}
            for record in (source, job):
                artifact = next(a for a in record["artifacts"] if a["name"] == "spectral-image.npy")
                assert artifact["sha256"] in checksums
        # An ordinary upload cannot forge a verified projection result or its lineage.
        artifact = image["artifacts"][0]
        metadata = copy.deepcopy(job["result"]["spectral_image_metadata"])
        forged = client.post("/api/v1/spectral_images/import", data={
            "metadata": json.dumps(metadata),
        }, files={"image": (
            "input.npy", service.registry.blob_path(artifact["sha256"]).read_bytes(),
            "application/octet-stream",
        )})
        assert forged.status_code == 422, forged.text
        assert sdk.get("workbench")["counts"] == counts
        assert sdk.get("scene_renders/" + source["id"]) == source


def test_projection_cancel_keeps_source_and_sdk_timeout_keeps_job(projection_source):  # noqa: F811
    service, assembly, source = projection_source
    target = target_camera(service, assembly)
    payload = draft(service, source, target)
    with client_for(service) as client:
        client.headers["X-Platform-Token"] = client.get("/api/v1/session").json()["token"]
        sdk = PlatformClient.__new__(PlatformClient)
        sdk.http = client
        job = sdk.project_scene_radiance(sdk.preview_scene_projection(payload))
        with pytest.raises(TimeoutError, match=job["id"]):
            sdk.wait_scene_projection(job["id"], timeout=0)
        assert sdk.get("scene_projections/" + job["id"])["status"] == "queued"
        queued_bundle = client.get("/api/v1/runs/" + job["id"] + "/bundle")
        assert queued_bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(queued_bundle.content)) as z:
            frozen = sdk.get("scene_projections/" + job["id"])
            for artifact in frozen["input_artifacts"]:
                original = z.read("artifacts/" + artifact["sha256"] + "/" + artifact["name"])
                assert hashlib.sha256(original).hexdigest() == artifact["sha256"]
        # Kind-specific cancellation must not cancel a different kind of job.
        wrong = client.post("/api/v1/scene_projections/" + source["id"] + "/cancel", json={})
        assert wrong.status_code == 422, wrong.text
        cancelled = client.post("/api/v1/scene_projections/" + job["id"] + "/cancel", json={})
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        denied = client.post("/api/v1/scene_projection_publications", json={
            "job_id": job["id"], "name": "Not completed",
        })
        assert denied.status_code == 422, denied.text
        bundle = client.get("/api/v1/scene_projections/" + job["id"] + "/bundle")
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as z:
            assert any(n.endswith("/spectral-radiance.npy") for n in z.namelist())
            assert any(n.endswith("/scene-projection-input.json") for n in z.namelist())
        assert sdk.get("scene_renders/" + source["id"]) == source
