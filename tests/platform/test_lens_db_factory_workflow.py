"""An actual reviewed Lens revision reaches two source-bound Factory inputs."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from camera_platform import lens_db_factory_publication as publication
from camera_platform import lens_db_factory_workflow as workflow
from camera_platform.lens_db_factory_workflow import plan, submit, template
from camera_platform.registry import ConflictError, canonical, digest
from camera_platform.service import PlatformService

ROOT = Path(__file__).resolve().parents[2]
REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"
CSV_SHA256 = "f3f4790fc4135501c2921ceb532cb6af764b991e3df5fe9c805953deae5779bd"


@pytest.fixture
def specification():
    if not (ROOT / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("The reviewed Lens catalog is not bundled in this test checkout")
    request = template()
    request.update(
        revision_id=REVISION,
        reviewed_csv_sha256=CSV_SHA256,
        wavelength_nm=[550.0, 650.0],
        pupil_samples=54,
        kernel_size=17,
        acknowledge_unverified_optics=True,
    )
    request["psf_prescription_sampling"].update(polar_rings=24, azimuth_samples=24)
    request["radiometry_sampling"].update(polar_rings=40, azimuth_samples=28)
    return request


def test_real_revision_plan_keeps_both_full_requests_and_source(tmp_path, specification):
    service = PlatformService(tmp_path, workspace=ROOT)
    result = plan(service, specification)
    assert result["source"]["revision_id"] == REVISION
    assert result["source"]["reviewed_csv_sha256"] == CSV_SHA256
    assert result["radiometry_request"]["sampling"]["polar_rings"] == 40
    assert result["psf_request"]["prescription"]["sampling"]["polar_rings"] == 24
    binding = result["physical_prescription_binding"]
    assert binding["radiometry_request_sha256"] == digest(result["radiometry_request"])
    assert binding["psf_prescription_request_sha256"] == digest(
        result["psf_request"]["prescription"]
    )
    assert result["commercial_release_allowed"] is False
    assert result["physical_validation"] == "not_established"


def test_reviewed_source_hash_and_plan_change_fail_before_any_write(tmp_path, specification):
    service = PlatformService(tmp_path, workspace=ROOT)
    wrong = {**specification, "reviewed_csv_sha256": "0" * 64}
    with pytest.raises(ValueError):
        plan(service, wrong)
    planned = plan(service, specification)
    changed = {**specification, "radiometry_sampling": {
        **specification["radiometry_sampling"], "polar_rings": 39,
    }}
    with pytest.raises(ValueError, match="changed after plan review"):
        submit(service, changed, expected_plan_sha256=planned["plan_sha256"])
    assert not service.registry.list("assets")
    assert not service.registry.list_runs()


def test_submit_creates_reusable_draft_asset_and_two_jobs(tmp_path, specification):
    service = PlatformService(tmp_path, workspace=ROOT)
    planned = plan(service, specification)
    first = submit(service, specification, expected_plan_sha256=planned["plan_sha256"])
    assert first["commercial_release_allowed"] is False
    asset = service.registry.get("assets", first["asset_id"])
    assert asset["approval_status"] == "draft"
    assert asset["properties"]["source"]["reviewed_csv_sha256"] == CSV_SHA256
    assert asset["properties"]["reviewed_csv_artifact_sha256"] == CSV_SHA256
    snapshot_path = service.registry.blob_path(
        asset["properties"]["source_snapshot_artifact_sha256"]
    )
    source_snapshot = json.loads(snapshot_path.read_text())
    assert source_snapshot["source"] == planned["source"]
    assert source_snapshot["reviewed_csv_artifact"]["sha256"] == CSV_SHA256
    artifacts = asset["lens_db_source_artifacts"]
    assert [item["name"] for item in artifacts] == [
        "reviewed-prescription.csv", "lens-db-source-snapshot.json"
    ]
    assert [item["sha256"] for item in artifacts] == [
        CSV_SHA256, asset["properties"]["source_snapshot_artifact_sha256"]
    ]
    reopened = PlatformService(tmp_path, workspace=ROOT)
    for artifact in artifacts:
        owner = reopened.registry.artifact_owner(artifact["sha256"])
        assert owner["kind"] == "assets"
        assert owner["owner"]["id"] == asset["id"]
        assert owner["descriptor"] == artifact
        assert owner["path"].read_bytes() == service.registry.blob_path(
            artifact["sha256"]
        ).read_bytes()
    for kind, plugin in (("radiometry", "rayoptics_lens_v1"), ("psf", "prescription_psf_v1")):
        job = service.registry.get_run(first["job_ids"][kind])
        assert job["plugin_id"] == plugin
        assert job["asset_snapshot"]["id"] == asset["id"]
        assert job["request"] == planned[kind + "_request"]
        assert job["status"] == "queued"
    second = submit(service, specification, expected_plan_sha256=planned["plan_sha256"])
    assert second["asset_id"] == first["asset_id"]
    assert second["job_ids"] == first["job_ids"]
    assert len(service.registry.list("assets")) == 1


def test_shared_csv_bytes_keep_each_lens_revision_exactly_bound(tmp_path, specification):
    service = PlatformService(tmp_path, workspace=ROOT)
    planned = plan(service, specification)
    submitted = submit(service, specification, expected_plan_sha256=planned["plan_sha256"])
    original = service.registry.get("assets", submitted["asset_id"])
    csv = original["lens_db_source_artifacts"][0]

    # A second typed Lens revision may legitimately refer to identical reviewed
    # CSV bytes. Its newer registration becomes the public artifact owner.
    other = copy.deepcopy(original)
    for key in ("id", "created_at", "content_hash"):
        other.pop(key)
    second_revision = "another-reviewed-revision"
    other["source_uri"] = "lens-db://revision/" + second_revision
    other["properties"]["source"]["revision_id"] = second_revision
    second_snapshot = service.registry.store_blob(
        canonical({"revision_id": second_revision}).encode(),
        "lens-db-source-snapshot.json", "application/json",
    )
    other["properties"]["source_snapshot_artifact_sha256"] = second_snapshot["sha256"]
    other["lens_db_source_artifacts"] = [csv, second_snapshot]
    other = service.registry.put("assets", other, identity="asset-second-lens-revision")

    assert service.registry.artifact_owner(csv["sha256"])["owner"]["id"] == other["id"]
    exact = service.registry.artifact_binding(
        csv["sha256"], owner_kind="assets", owner_id=original["id"]
    )
    assert exact["owner"]["id"] == original["id"]
    assert exact["descriptor"] == csv
    assert submit(service, specification, expected_plan_sha256=planned["plan_sha256"])[
        "asset_id"
    ] == original["id"]
    assert publication._source_asset(service, planned, original["id"])["id"] == original["id"]

    # Removing only the original binding must fail closed even though the same
    # bytes remain available through the second revision.
    with service.registry.connection() as conn:
        conn.execute(
            "DELETE FROM artifact_owners WHERE artifact_id=? AND owner_kind='assets' "
            "AND owner_id=?", (csv["sha256"], original["id"]),
        )
    with pytest.raises(KeyError, match="exact owner"):
        submit(service, specification, expected_plan_sha256=planned["plan_sha256"])
    with pytest.raises(KeyError, match="exact owner"):
        publication._source_asset(service, planned, original["id"])


def test_partial_factory_pair_reports_first_job_and_retry_reuses_it(
    tmp_path, specification, monkeypatch,
):
    service = PlatformService(tmp_path, workspace=ROOT)
    planned = plan(service, specification)
    real_submit = workflow.factory.submit

    def fail_second(current_service, payload):
        if payload["plugin_id"] == "prescription_psf_v1":
            raise RuntimeError("transient PSF submission failure")
        return real_submit(current_service, payload)

    monkeypatch.setattr(workflow.factory, "submit", fail_second)
    with pytest.raises(ConflictError, match="partial: radiometry Job") as failure:
        submit(service, specification, expected_plan_sha256=planned["plan_sha256"])
    first = service.registry.list_runs()
    assert len(first) == 1 and first[0]["plugin_id"] == "rayoptics_lens_v1"
    assert first[0]["id"] in str(failure.value)
    monkeypatch.setattr(workflow.factory, "submit", real_submit)
    recovered = submit(service, specification, expected_plan_sha256=planned["plan_sha256"])
    assert recovered["job_ids"]["radiometry"] == first[0]["id"]
    assert len(service.registry.list_runs()) == 2


def test_generic_asset_properties_do_not_grant_artifact_access(tmp_path):
    service = PlatformService(tmp_path, workspace=ROOT)
    descriptor = service.registry.store_blob(b"private source bytes", "source.txt", "text/plain")
    service.create_asset({
        "name": "ordinary asset", "kind": "lens", "source_kind": "estimated",
        "source_uri": "fixture://ordinary", "license": "test",
        "method_and_version": "test:1", "physical_instance": None,
        "parents": [], "properties": {"lens_db_source_artifacts": [descriptor]},
    })
    with pytest.raises(KeyError, match="not associated"):
        service.registry.artifact_owner(descriptor["sha256"])
