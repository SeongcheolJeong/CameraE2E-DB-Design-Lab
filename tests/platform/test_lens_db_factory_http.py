"""A reviewed Lens DB revision is reachable through the public Factory workflow."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient

ROOT = Path(__file__).resolve().parents[2]
REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"
CSV_SHA256 = "f3f4790fc4135501c2921ceb532cb6af764b991e3df5fe9c805953deae5779bd"


def test_http_plan_submit_and_reopened_source_download(tmp_path):
    if not (ROOT / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("The reviewed Lens catalog is not bundled in this test checkout")
    state = tmp_path / "platform"
    app = create_app(state, workspace=ROOT, start_worker=False, seed_demo=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        template = client.get("/api/v1/lens-db-factory-pairs/template")
        assert template.status_code == 200, template.text
        specification = template.json()
        specification.update(
            revision_id=REVISION,
            reviewed_csv_sha256=CSV_SHA256,
            wavelength_nm=[550.0, 650.0],
            acknowledge_unverified_optics=True,
        )
        token = client.get("/api/v1/session").json()["token"]
        headers = {"x-platform-token": token}
        planned = client.post(
            "/api/v1/lens-db-factory-pairs/plan", json=specification, headers=headers,
        )
        assert planned.status_code == 200, planned.text
        plan = planned.json()
        assert plan["source"]["revision_id"] == REVISION
        assert plan["physical_validation"] == "not_established"
        assert plan["product_ranking_allowed"] is False
        stale = client.post(
            "/api/v1/lens-db-factory-pairs",
            json={"specification": specification, "expected_plan_sha256": "0" * 64},
            headers=headers,
        )
        assert stale.status_code == 422
        submitted = client.post(
            "/api/v1/lens-db-factory-pairs",
            json={
                "specification": specification,
                "expected_plan_sha256": plan["plan_sha256"],
            },
            headers=headers,
        )
        assert submitted.status_code == 202, submitted.text
        pair = submitted.json()
        assert set(pair["job_ids"]) == {"radiometry", "psf"}
        assert pair["commercial_release_allowed"] is False
        for identity in pair["job_ids"].values():
            job = client.get("/api/v1/factory/jobs/" + identity)
            assert job.status_code == 200 and job.json()["status"] == "queued"
        publication = {
            "specification": specification,
            "expected_plan_sha256": plan["plan_sha256"],
            "asset_id": pair["asset_id"],
            "job_ids": pair["job_ids"],
            "conditions": {"f_number": {"value": 2.0, "unit": "1"}},
            "radiometry_name": "queued radiometry draft",
            "radiometry_revision": "1",
            "psf_name": "queued PSF draft",
            "psf_revision": "1",
        }
        refused = client.post(
            "/api/v1/lens-db-factory-pairs/models", json=publication, headers=headers,
        )
        assert refused.status_code == 422 and "succeeded PROPOSED" in refused.text
        sdk = object.__new__(PlatformClient)
        sdk.http = client
        client.headers.update(headers)
        with pytest.raises(httpx.HTTPStatusError) as error:
            sdk.publish_lens_db_factory_pair(publication)
        assert error.value.response.status_code == 422
        assert "succeeded PROPOSED" in error.value.response.text
        assert app.state.service.registry.list("models") == []
        asset = app.state.service.registry.get("assets", pair["asset_id"])
        source_artifacts = asset["lens_db_source_artifacts"]

    restarted = create_app(state, workspace=ROOT, start_worker=False, seed_demo=False)
    with TestClient(restarted, raise_server_exceptions=False) as client:
        for descriptor in source_artifacts:
            downloaded = client.get(descriptor["url"])
            assert downloaded.status_code == 200, downloaded.text
            assert hashlib.sha256(downloaded.content).hexdigest() == descriptor["sha256"]
