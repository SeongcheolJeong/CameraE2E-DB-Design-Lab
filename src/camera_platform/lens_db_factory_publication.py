"""Explicit, source-bound publication of a Lens DB Factory pair as research drafts.

The two Factory jobs remain independent numerical characterizations. Publication
does not turn a patent-table aperture assumption into physical or commercial
validation. A retry may reuse only byte-for-byte equivalent draft models.
"""

from __future__ import annotations

import copy
import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from . import factory
from . import lens_db_factory_workflow as workflow
from .contracts import Contract, ModelCreate
from .gain_semantics import validate_model_declaration
from .lens_catalog_bridge import _revision_source
from .prescription_identity import sampling_independent_binding
from .registry import ConflictError, canonical
from .uncertainty import validate_record

SCHEMA = "camera_lens_db_factory_publication_v1"


class FactoryPairPublicationCreate(Contract):
    specification: dict[str, Any]
    expected_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    asset_id: str = Field(min_length=1)
    job_ids: dict[str, str]
    conditions: dict[str, Any]
    radiometry_name: str = Field(min_length=1, max_length=200)
    radiometry_revision: str = Field(min_length=1, max_length=80)
    psf_name: str = Field(min_length=1, max_length=200)
    psf_revision: str = Field(min_length=1, max_length=80)
    condition_scope: Literal["all_declared", "optical_response"] = "optical_response"

    @model_validator(mode="after")
    def exact_job_pair(self):
        if (
            set(self.job_ids) != {"radiometry", "psf"}
            or any(not isinstance(value, str) or not value for value in self.job_ids.values())
            or self.job_ids["radiometry"] == self.job_ids["psf"]
        ):
            raise ValueError("Declare two distinct radiometry and psf Factory job IDs")
        return self


class PartialPairPublicationError(ConflictError):
    """The verified parent draft exists; retry the identical request for the child."""

    def __init__(self, parent_model_id: str, cause: Exception):
        self.parent_model_id = parent_model_id
        super().__init__(
            "Lens DB pair publication is partial: verified radiometry draft "
            f"{parent_model_id} exists; PSF draft failed ({type(cause).__name__}: {cause}). "
            "Retry the identical publication request; the parent will be reused."
        )


@contextmanager
def _publication_lock(service):
    # Serialize idempotent lookup and writes across API workers using this registry.
    with (Path(service.registry.directory) / ".lens-db-pair-publication.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _source_asset(service, planned: dict, asset_id: str) -> dict:
    asset = service.registry.get("assets", asset_id)
    source = planned["source"]
    props = asset.get("properties", {})
    if not (
        asset.get("approval_status") == "draft"
        and asset.get("kind") == "lens"
        and asset.get("source_kind") == "estimated"
        and asset.get("source_uri") == "lens-db://revision/" + source["revision_id"]
        and asset.get("license") == workflow.RIGHTS
        and asset.get("method_and_version") == "lens_db_wavefront_bridge:1"
        and asset.get("physical_instance") is None
        and asset.get("parents") == []
        and props.get("source") == source
        and props.get("physical_validation") == "not_established"
        and props.get("commercial_release_allowed") is False
    ):
        raise ValueError("Lens DB source asset is not the exact unvalidated planned revision")
    artifacts = asset.get("lens_db_source_artifacts")
    if not (isinstance(artifacts, list) and len(artifacts) == 2):
        raise ValueError("Lens DB source asset lacks its two pinned source artifacts")
    by_name = {item.get("name"): item for item in artifacts}
    if set(by_name) != {"reviewed-prescription.csv", "lens-db-source-snapshot.json"}:
        raise ValueError("Lens DB source artifacts differ from the submitted pair")
    csv, snapshot = by_name["reviewed-prescription.csv"], by_name["lens-db-source-snapshot.json"]
    if (
        csv["sha256"] != props.get("reviewed_csv_artifact_sha256")
        or csv["sha256"] != source["reviewed_csv_sha256"]
        or snapshot["sha256"] != props.get("source_snapshot_artifact_sha256")
    ):
        raise ValueError("Lens DB source artifact hashes differ from the planned revision")
    for artifact in artifacts:
        owner = service.registry.artifact_binding(
            artifact["sha256"], owner_kind="assets", owner_id=asset_id
        )
        if (
            owner["kind"] != "assets"
            or owner["owner"]["id"] != asset_id
            or owner["descriptor"] != artifact
        ):
            raise ValueError("Lens DB source artifact lacks exact asset ownership")
        if service.registry.blob_path(artifact["sha256"]).stat().st_size != artifact["bytes"]:
            raise ValueError("Lens DB source artifact size differs")
    saved = json.loads(service.registry.blob_path(snapshot["sha256"]).read_bytes())
    _, current_revision_snapshot, _, _ = _revision_source(
        source["revision_id"], service.workspace / "camerae2e_db"
    )
    if saved != {
        "schema": "camera_lens_db_factory_source_snapshot_v1",
        "source": source,
        "catalog_revision_snapshot": current_revision_snapshot,
        "reviewed_csv_artifact": csv,
        "assumptions": planned["assumptions"],
        "source_semantics_review": planned["source_semantics_review"],
        "source_validation_warning_codes": planned["source_validation_warning_codes"],
        "translation_audit": planned["translation_audit"],
        "physical_validation": "not_established",
        "commercial_release_allowed": False,
    }:
        raise ValueError("Lens DB source snapshot differs from the replanned revision")
    if (
        len(
            [
                a
                for a in service.registry.list("assets")
                if a.get("source_uri") == asset["source_uri"]
            ]
        )
        != 1
    ):
        raise ValueError("Lens DB source revision has ambiguous registered assets")
    return asset


def _job(service, job_id: str, *, label: str, asset: dict, planned: dict) -> dict:
    plugin = {"radiometry": "rayoptics_lens_v1", "psf": "prescription_psf_v1"}[label]
    job = factory.get_job(service, job_id)
    request = planned[label + "_request"]
    result = job.get("result") or {}
    if not (
        job["status"] == "succeeded"
        and job.get("plugin_id") == plugin
        and job.get("asset_id") == asset["id"]
        and job.get("asset_snapshot") == asset
        and job.get("request") == request
        and job.get("name") == "Lens DB " + planned["revision_id"] + " " + label
        and job.get("source_kind") == "estimated"
        and job.get("source_uri") == asset["source_uri"]
        and job.get("license") == workflow.RIGHTS
        and job.get("method_and_version") == "lens_db_wavefront_bridge:1+factory:1"
        and job.get("parents") == []
        and job.get("physical_instance") is None
        and "force" not in job
        and result.get("summary", {}).get("status") == "PROPOSED"
        and result.get("model_candidate") is not None
        and result.get("physical_validation") == "not_established"
        and result.get("approval_status") == "unapproved"
        and result.get("artifacts") == job.get("artifacts")
    ):
        raise ValueError(
            f"{label} Factory job is not the exact succeeded PROPOSED source-bound job"
        )
    from .service import environment_fingerprint

    if (
        job.get("environment") != service.loaded_environment
        or job["environment"] != environment_fingerprint()
    ):
        raise ValueError(f"{label} Factory job used another executable environment")
    if job.get("backend") != factory.backend_identity(plugin):
        raise ValueError(f"{label} Factory backend differs from its frozen identity")
    artifacts = job["artifacts"]
    if (
        not isinstance(artifacts, list)
        or not artifacts
        or job.get("input_artifacts") != artifacts[:1]
    ):
        raise ValueError(f"{label} Factory input artifact is missing")
    for item in artifacts:
        path = service.registry.blob_path(item["sha256"])
        if path.stat().st_size != item["bytes"]:
            raise ValueError(f"{label} Factory artifact size differs")
    evidence = [item for item in artifacts if item["name"] == "characterization.json"]
    if (
        len(evidence) != 1
        or service.registry.blob_path(evidence[0]["sha256"]).read_bytes()
        != canonical(
            {
                "summary": result["summary"],
                "model_candidate": result["model_candidate"],
            }
        ).encode()
    ):
        raise ValueError(f"{label} Factory candidate differs from its saved characterization")
    frozen_fields = {
        k: copy.deepcopy(v)
        for k, v in job.items()
        if k
        in {
            "name",
            "asset_id",
            "plugin_id",
            "source_kind",
            "source_uri",
            "license",
            "method_and_version",
            "physical_instance",
            "parents",
            "request",
        }
    }
    if (
        service.registry.blob_path(artifacts[0]["sha256"]).read_bytes()
        != canonical(frozen_fields).encode()
    ):
        raise ValueError(f"{label} Factory input differs from its submitted request")
    return job


def _model_data(service, data: dict) -> dict:
    validated = ModelCreate.model_validate(data).model_dump()
    service._validate_model_payload(validated)
    validate_model_declaration(validated)
    validate_record(validated)
    return validated


def _existing_model(service, expected: dict) -> dict | None:
    matches = []
    for model in service.registry.list("models"):
        if model.get("asset_id") != expected["asset_id"]:
            continue
        if all(model.get(key) == value for key, value in expected.items()):
            if (
                model.get("approval_status") != "draft"
                or model.get("factory", {}).get("physical_validation") != "not_established"
            ):
                raise ConflictError(
                    "Matching Lens DB model has a changed validation/approval state"
                )
            matches.append(model)
    if len(matches) > 1:
        raise ConflictError("Ambiguous duplicate Lens DB draft models; resolve before retry")
    if matches:
        model = matches[0]
        if model.get("factory_job_id"):
            factory.verify_model_link(service, model)
        else:
            factory.verify_psf_model_link(service, model)
        from .native_model_evidence import verify_models

        verify_models(service, [model])
        return model
    return None


def _prepared(
    service, payload: FactoryPairPublicationCreate
) -> tuple[dict, dict, dict, dict, dict]:
    planned = workflow.plan(service, payload.specification)
    if planned["plan_sha256"] != payload.expected_plan_sha256:
        raise ValueError("Lens DB source or optical requests changed after plan review")
    asset = _source_asset(service, planned, payload.asset_id)
    radiometry = _job(
        service, payload.job_ids["radiometry"], label="radiometry", asset=asset, planned=planned
    )
    psf = _job(service, payload.job_ids["psf"], label="psf", asset=asset, planned=planned)
    binding = sampling_independent_binding(radiometry["request"], psf["request"]["prescription"])
    if binding != planned["physical_prescription_binding"]:
        raise ValueError("Factory jobs do not preserve the replanned physical prescription binding")
    factory.FactoryModelCreate.model_validate(
        {
            "job_id": radiometry["id"],
            "name": payload.radiometry_name,
            "revision": payload.radiometry_revision,
            "conditions": payload.conditions,
        }
    )
    factory.FactoryModelCreate.model_validate(
        {
            "job_id": psf["id"],
            "name": payload.psf_name,
            "revision": payload.psf_revision,
            "conditions": payload.conditions,
            "base_lens_model_id": "preflight-parent",
            "condition_scope": payload.condition_scope,
        }
    )
    if payload.conditions.get("f_number") != {
        "value": psf["result"]["summary"]["geometric_reference_f_number"],
        "unit": "1",
    }:
        raise ValueError("Declare the exact preserved PSF geometric reference f_number")
    if payload.condition_scope == "optical_response" and set(payload.conditions) & {
        "exposure_s",
        "gain",
    }:
        raise ValueError(
            "Optical response reuse cannot inherit fixed exposure_s or gain from its parent"
        )
    base = _model_data(
        service,
        {
            **factory.candidate_fields(radiometry),
            "name": payload.radiometry_name,
            "revision": payload.radiometry_revision,
            "valid_domain": factory.application_domain(payload.conditions),
            "factory_application_conditions": payload.conditions,
        },
    )
    factory.verify_model_link(service, base)
    # The synthetic ID is used only to preflight every child payload gate; no
    # model write occurs until both normal Factory compositions are accepted.
    synthetic_parent = {**base, "id": "preflight-parent"}
    composite = _model_data(
        service,
        {
            **factory.psf_candidate_fields(
                psf,
                synthetic_parent,
                payload.conditions,
                payload.condition_scope,
                service=service,
            ),
            "name": payload.psf_name,
            "revision": payload.psf_revision,
        },
    )
    if composite["asset_id"] != asset["id"] or composite["parents"] != ["preflight-parent"]:
        raise ValueError("PSF candidate lost the exact parent Lens source binding")
    return planned, asset, radiometry, psf, base


def publish_pair(service, payload: dict[str, Any]) -> dict[str, Any]:
    """Preflight both jobs, then publish/reuse the exact two draft Lens models.

    If the second write fails, a PartialPairPublicationError exposes the parent
    ID and an identical retry reuses it. No physical or commercial approval is
    granted by either successful numerical job or publication.
    """
    request = FactoryPairPublicationCreate.model_validate(payload)
    with _publication_lock(service):
        planned, asset, radiometry, psf, base_data = _prepared(service, request)
        parent = _existing_model(service, base_data)
        if parent is None:
            parent = factory.publish(
                service,
                {
                    "job_id": radiometry["id"],
                    "name": request.radiometry_name,
                    "revision": request.radiometry_revision,
                    "conditions": request.conditions,
                },
            )
        try:
            child_data = _model_data(
                service,
                {
                    **factory.psf_candidate_fields(
                        psf,
                        parent,
                        request.conditions,
                        request.condition_scope,
                        service=service,
                    ),
                    "name": request.psf_name,
                    "revision": request.psf_revision,
                },
            )
            child = _existing_model(service, child_data)
            if child is None:
                child = factory.publish(
                    service,
                    {
                        "job_id": psf["id"],
                        "base_lens_model_id": parent["id"],
                        "name": request.psf_name,
                        "revision": request.psf_revision,
                        "conditions": request.conditions,
                        "condition_scope": request.condition_scope,
                    },
                )
        except Exception as exc:
            raise PartialPairPublicationError(parent["id"], exc) from exc
        return {
            "schema": SCHEMA,
            "asset_id": asset["id"],
            "source": planned["source"],
            "plan_sha256": planned["plan_sha256"],
            "job_ids": request.job_ids,
            "model_ids": {"radiometry": parent["id"], "psf": child["id"]},
            "approval_status": "draft",
            "physical_validation": "not_established",
            "product_ranking_allowed": False,
            "commercial_release_allowed": False,
            "limitation": workflow.LIMITATION,
        }
