"""Explicit Lens DB publication stays source-bound and recovers partial drafts."""

from __future__ import annotations

from pathlib import Path

import pytest

from camera_platform import lens_db_factory_publication as publication
from camera_platform import lens_db_factory_workflow as workflow
from camera_platform.service import PlatformService

ROOT = Path(__file__).resolve().parents[2]
REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"
CSV_SHA256 = "f3f4790fc4135501c2921ceb532cb6af764b991e3df5fe9c805953deae5779bd"


@pytest.fixture
def submitted(tmp_path):
    if not (ROOT / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("The reviewed Lens catalog is not bundled in this checkout")
    service = PlatformService(tmp_path, workspace=ROOT)
    specification = workflow.template()
    specification.update(
        revision_id=REVISION,
        reviewed_csv_sha256=CSV_SHA256,
        wavelength_nm=[550.0, 650.0],
        pupil_samples=54,
        kernel_size=17,
        acknowledge_unverified_optics=True,
    )
    specification["psf_prescription_sampling"].update(polar_rings=24, azimuth_samples=24)
    specification["radiometry_sampling"].update(polar_rings=40, azimuth_samples=28)
    planned = workflow.plan(service, specification)
    pair = workflow.submit(service, specification, expected_plan_sha256=planned["plan_sha256"])
    request = {
        "specification": specification,
        "expected_plan_sha256": planned["plan_sha256"],
        "asset_id": pair["asset_id"],
        "job_ids": pair["job_ids"],
        "conditions": {"f_number": {"value": 2.0, "unit": "1"}},
        "radiometry_name": "p0014 radiometry research draft",
        "radiometry_revision": "1",
        "psf_name": "p0014 PSF research draft",
        "psf_revision": "1",
        "condition_scope": "optical_response",
    }
    return service, request


def test_queued_pair_cannot_publish_any_model(submitted):
    service, request = submitted
    with pytest.raises(ValueError, match="succeeded PROPOSED"):
        publication.publish_pair(service, request)
    assert service.registry.list("models") == []


def test_publication_replans_and_requires_exact_asset_and_job_binding(submitted):
    service, request = submitted
    wrong_plan = {**request, "expected_plan_sha256": "0" * 64}
    with pytest.raises(ValueError, match="changed after plan review"):
        publication.publish_pair(service, wrong_plan)
    wrong_asset = {**request, "asset_id": "asset-does-not-exist"}
    with pytest.raises(KeyError):
        publication.publish_pair(service, wrong_asset)
    wrong_jobs = {
        **request,
        "job_ids": {
            "radiometry": request["job_ids"]["psf"],
            "psf": request["job_ids"]["radiometry"],
        },
    }
    with pytest.raises(ValueError, match="succeeded PROPOSED"):
        publication.publish_pair(service, wrong_jobs)
    assert service.registry.list("models") == []


def test_publication_requires_exclusive_optical_conditions_and_names():
    request = {
        "specification": {},
        "expected_plan_sha256": "0" * 64,
        "asset_id": "asset-1",
        "job_ids": {"radiometry": "run-a", "psf": "run-b"},
        "conditions": {"f_number": {"value": 2.0, "unit": "1"}},
        "radiometry_name": "",
        "radiometry_revision": "1",
        "psf_name": "PSF",
        "psf_revision": "1",
    }
    with pytest.raises(ValueError):
        publication.FactoryPairPublicationCreate.model_validate(request)
    request["radiometry_name"] = "RI"
    request["job_ids"] = {"radiometry": "run-a", "psf": "run-a"}
    with pytest.raises(ValueError, match="distinct"):
        publication.FactoryPairPublicationCreate.model_validate(request)


def test_child_write_failure_reports_parent_and_exact_retry_reuses_it(monkeypatch, tmp_path):
    service = PlatformService(tmp_path)
    request = {
        "specification": {},
        "expected_plan_sha256": "0" * 64,
        "asset_id": "asset-1",
        "job_ids": {"radiometry": "run-a", "psf": "run-b"},
        "conditions": {"f_number": {"value": 2.0, "unit": "1"}},
        "radiometry_name": "RI",
        "radiometry_revision": "1",
        "psf_name": "PSF",
        "psf_revision": "1",
    }
    monkeypatch.setattr(
        publication,
        "_prepared",
        lambda *_: (
            {"source": {"revision_id": "revision-1"}, "plan_sha256": "0" * 64},
            {"id": "asset-1"},
            {"id": "run-a"},
            {"id": "run-b"},
            {"name": "RI"},
        ),
    )
    saved = {}
    events = []

    def existing(_service, expected):
        return saved.get(expected["name"])

    def make(_service, payload):
        if payload["job_id"] == "run-a":
            events.append("parent")
            result = {"id": "model-parent", "name": "RI"}
            saved["RI"] = result
            return result
        events.append("child")
        if events.count("child") == 1:
            raise RuntimeError("one-time child write failure")
        result = {"id": "model-child", "name": "PSF"}
        saved["PSF"] = result
        return result

    monkeypatch.setattr(publication, "_existing_model", existing)
    monkeypatch.setattr(publication.factory, "publish", make)
    monkeypatch.setattr(
        publication.factory,
        "psf_candidate_fields",
        lambda *_args, **_kwargs: {
            "name": "PSF",
            "parents": ["model-parent"],
        },
    )
    monkeypatch.setattr(publication, "_model_data", lambda _service, data: data)
    with pytest.raises(publication.PartialPairPublicationError) as failure:
        publication.publish_pair(service, request)
    assert failure.value.parent_model_id == "model-parent"
    result = publication.publish_pair(service, request)
    assert result["model_ids"] == {"radiometry": "model-parent", "psf": "model-child"}
    assert events == ["parent", "child", "child"]
    assert result["approval_status"] == "draft"
    assert result["physical_validation"] == "not_established"
    assert result["commercial_release_allowed"] is False
