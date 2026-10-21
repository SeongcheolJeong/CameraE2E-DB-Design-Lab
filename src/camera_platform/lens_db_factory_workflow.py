"""Source-pinned Lens DB revision to ordinary optical Factory jobs.

The bridge does not approve a patent prescription or relax either solver's
numerical gates. It only turns one reviewed revision into two independently
sampled, immutable Factory inputs and retains the source identity in a draft
asset. The regular Factory, Worker and publication contracts remain in force.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

from . import factory, lens_factory, prescription_psf
from .contracts import AssetCreate
from .lens_catalog_bridge import _revision_source
from .lens_db_wavefront_bridge import (
    APERTURE_HYPOTHESIS,
    _reviewed_csv,
    db_wavefront_preflight,
)
from .prescription_identity import sampling_independent_binding
from .registry import ConflictError, canonical, digest

SCHEMA = "camera_lens_db_factory_pair_v1"
FIELDS = {
    "schema", "revision_id", "reviewed_csv_sha256", "aperture_hypothesis",
    "wavelength_nm", "field_x_mm", "field_y_mm", "pixel_pitch_um",
    "radiometry_sampling", "psf_prescription_sampling", "pupil_samples",
    "kernel_size", "subpixel_samples", "acknowledge_unverified_optics",
}
RIGHTS = "Internal research only; source redistribution rights not established"
LIMITATION = (
    "Patent-table effective diameter is an unverified clear-aperture proxy; "
    "nd/Vd dispersion, unity stop power and scalar diffraction are research "
    "assumptions. PROPOSED numerical results are not physical Lens accuracy, "
    "CRA/pixel coupling, product ranking or commercial release approval."
)


def template() -> dict[str, Any]:
    sampling = lens_factory.request_template(version=4)["sampling"]
    return {
        "schema": SCHEMA, "revision_id": "", "reviewed_csv_sha256": "",
        "aperture_hypothesis": APERTURE_HYPOTHESIS,
        "wavelength_nm": [450.0, 550.0, 650.0],
        "field_x_mm": [-0.25, 0.25], "field_y_mm": [-0.25, 0.25],
        "pixel_pitch_um": 3.0,
        "radiometry_sampling": copy.deepcopy(sampling),
        "psf_prescription_sampling": copy.deepcopy(sampling),
        "pupil_samples": 16, "kernel_size": 5, "subpixel_samples": 5,
        "acknowledge_unverified_optics": False,
    }


def plan(service, specification: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(specification, dict) or set(specification) != FIELDS:
        raise ValueError("Declare exactly the Lens DB Factory pair fields")
    if (
        specification["schema"] != SCHEMA
        or specification["acknowledge_unverified_optics"] is not True
    ):
        raise ValueError("Explicit unverified-optics acknowledgement is required")
    preflight = db_wavefront_preflight(
        specification["revision_id"],
        catalog_root=service.workspace / "camerae2e_db",
        reviewed_csv_sha256=specification["reviewed_csv_sha256"],
        aperture_hypothesis=specification["aperture_hypothesis"],
        wavelength_nm=specification["wavelength_nm"],
        field_x_mm=specification["field_x_mm"],
        field_y_mm=specification["field_y_mm"],
        pixel_pitch_um=specification["pixel_pitch_um"],
        pupil_samples=specification["pupil_samples"],
        kernel_size=specification["kernel_size"],
        subpixel_samples=specification["subpixel_samples"],
    )
    if preflight["status"] != "research_request_ready":
        raise ValueError("Lens DB optical request is blocked: " + canonical(preflight["blockers"]))
    psf_request = copy.deepcopy(preflight["psf_request"])
    psf_request["prescription"]["sampling"] = copy.deepcopy(
        specification["psf_prescription_sampling"]
    )
    radiometry_request = copy.deepcopy(psf_request["prescription"])
    radiometry_request["sampling"] = copy.deepcopy(specification["radiometry_sampling"])
    psf_request = prescription_psf.validate_request(psf_request)
    radiometry_request = lens_factory.validate_request(radiometry_request)
    binding = sampling_independent_binding(radiometry_request, psf_request["prescription"])
    for plugin_id in ("rayoptics_lens_v1", "prescription_psf_v1"):
        factory.backend_identity(plugin_id)
    result = {
        "schema": SCHEMA, "revision_id": specification["revision_id"],
        "source": preflight["source"],
        "source_semantics_review": preflight["source_semantics_review"],
        "source_validation_warning_codes": preflight["source_validation_warning_codes"],
        "translation_audit": preflight["translation_audit"],
        "assumptions": preflight["assumptions"],
        "radiometry_request": radiometry_request,
        "psf_request": psf_request,
        "physical_prescription_binding": binding,
        "physical_validation": "not_established",
        "product_ranking_allowed": False,
        "commercial_release_allowed": False,
        "limitation": LIMITATION,
    }
    return {**result, "plan_sha256": digest(result)}


def _source_asset(service, planned: dict[str, Any]) -> dict[str, Any]:
    source = planned["source"]
    uri = "lens-db://revision/" + source["revision_id"]
    revision, snapshot, db_path, db_sha256 = _revision_source(
        source["revision_id"], service.workspace / "camerae2e_db"
    )
    if (
        revision["content_sha256"] != source["revision_content_sha256"]
        or db_sha256 != source["source_database_sha256"]
    ):
        raise ValueError("Lens DB source changed between plan and asset registration")
    reviewed_relative = Path(source["reviewed_csv_relative_path"])
    if not reviewed_relative.parts or reviewed_relative.parts[0] != "reviewed_prescriptions":
        raise ValueError("Lens DB reviewed CSV path is outside the pinned source layout")
    _, _, csv_bytes = _reviewed_csv(
        db_path,
        Path(*reviewed_relative.parts[1:]).as_posix(),
        source["reviewed_csv_sha256"],
    )
    if hashlib.sha256(csv_bytes).hexdigest() != source["reviewed_csv_sha256"]:
        raise ValueError("Reviewed Lens CSV changed between plan and submission")
    csv_artifact = service.registry.store_blob(
        csv_bytes, "reviewed-prescription.csv", "text/csv"
    )
    source_snapshot = {
        "schema": "camera_lens_db_factory_source_snapshot_v1",
        "source": source,
        "catalog_revision_snapshot": snapshot,
        "reviewed_csv_artifact": csv_artifact,
        "assumptions": planned["assumptions"],
        "source_semantics_review": planned["source_semantics_review"],
        "source_validation_warning_codes": planned["source_validation_warning_codes"],
        "translation_audit": planned["translation_audit"],
        "physical_validation": "not_established",
        "commercial_release_allowed": False,
    }
    snapshot_artifact = service.registry.store_blob(
        canonical(source_snapshot).encode(), "lens-db-source-snapshot.json", "application/json"
    )
    props = {
        "source": source,
        "source_snapshot_artifact_sha256": snapshot_artifact["sha256"],
        "reviewed_csv_artifact_sha256": csv_artifact["sha256"],
        "physical_validation": "not_established",
        "commercial_release_allowed": False,
    }
    source_artifacts = [csv_artifact, snapshot_artifact]

    def same_source(asset):
        return (
            asset["source_uri"] == uri
            and asset["properties"] == props
            and asset.get("lens_db_source_artifacts") == source_artifacts
            and asset["approval_status"] == "draft"
        )

    matches = [asset for asset in service.registry.list("assets") if asset["source_uri"] == uri]
    if len(matches) > 1 or (matches and not same_source(matches[0])):
        raise ValueError("Lens DB revision has an ambiguous or changed registered source asset")

    def verified_asset(asset):
        for artifact in source_artifacts:
            binding = service.registry.artifact_binding(
                artifact["sha256"], owner_kind="assets", owner_id=asset["id"]
            )
            if binding["descriptor"] != artifact:
                raise ValueError("Lens DB source artifact differs from its exact asset binding")
        return asset

    if matches:
        return verified_asset(matches[0])
    payload = AssetCreate.model_validate({
        "name": "Lens DB prescription " + source["revision_id"],
        "kind": "lens", "description": LIMITATION,
        "source_kind": "estimated", "source_uri": uri, "license": RIGHTS,
        "method_and_version": "lens_db_wavefront_bridge:1",
        "physical_instance": None, "parents": [], "properties": props,
    }).model_dump()
    identity = "asset-lens-db-" + digest({
        "revision_id": source["revision_id"],
        "source_snapshot_sha256": snapshot_artifact["sha256"],
    })
    try:
        return verified_asset(service.registry.put(
            "assets", {**payload, "approval_status": "draft",
                       "lens_db_source_artifacts": source_artifacts},
            identity=identity,
        ))
    except ConflictError:
        # Two submissions can pass the list check concurrently. A stable ID
        # lets the loser recover only the exact same immutable source asset.
        existing = service.registry.get("assets", identity)
        if not same_source(existing):
            raise ValueError(
                "Lens DB source asset identity collided with different content"
            ) from None
        return verified_asset(existing)


def submit(service, specification: dict[str, Any], *, expected_plan_sha256: str) -> dict[str, Any]:
    """Submit both ordinary Factory jobs after rechecking the selected DB bytes."""
    from .service import environment_fingerprint

    planned = plan(service, specification)
    if expected_plan_sha256 != planned["plan_sha256"]:
        raise ValueError("Lens DB source or optical requests changed after plan review")
    if service.loaded_environment != environment_fingerprint():
        raise ValueError("Executable source changed after service startup")
    asset = _source_asset(service, planned)
    source = planned["source"]
    job_basis = {
        "asset_id": asset["id"], "source_kind": "estimated",
        "source_uri": asset["source_uri"], "license": RIGHTS,
        "method_and_version": "lens_db_wavefront_bridge:1+factory:1",
        "physical_instance": None, "parents": [], "force": False,
    }
    jobs = {}
    for label, plugin_id, request in (
        ("radiometry", "rayoptics_lens_v1", planned["radiometry_request"]),
        ("psf", "prescription_psf_v1", planned["psf_request"]),
    ):
        try:
            jobs[label] = factory.submit(service, {
                **job_basis, "name": "Lens DB " + source["revision_id"] + " " + label,
                "plugin_id": plugin_id, "request": request,
            })
        except Exception as exc:
            if not jobs:
                raise
            first = jobs["radiometry"]["id"]
            raise ConflictError(
                f"Lens DB Factory pair is partial: radiometry Job {first} was submitted, "
                f"{label} submission failed ({type(exc).__name__}: {exc}). "
                "Retry the same reviewed plan after fixing the cause; the first Job is reusable."
            ) from exc
    return {
        "schema": SCHEMA, "asset_id": asset["id"], "source": source,
        "plan_sha256": planned["plan_sha256"],
        "physical_prescription_binding": planned["physical_prescription_binding"],
        "job_ids": {key: job["id"] for key, job in jobs.items()},
        "physical_validation": "not_established",
        "commercial_release_allowed": False,
        "limitation": LIMITATION,
    }


def install_routes(app: Any, service: Any) -> None:
    """Expose the same pinned DB-to-Factory contract to the SDK and UI."""

    @app.get("/api/v1/lens-db-factory-pairs/template", tags=["model_factory"])
    def lens_db_factory_pair_template():
        return template()

    @app.post("/api/v1/lens-db-factory-pairs/plan", tags=["model_factory"])
    def lens_db_factory_pair_plan(specification: dict[str, Any]):
        return plan(service, specification)

    @app.post("/api/v1/lens-db-factory-pairs", status_code=202, tags=["model_factory"])
    def lens_db_factory_pair_submit(payload: dict[str, Any]):
        if set(payload) != {"specification", "expected_plan_sha256"}:
            raise ValueError("Declare specification and expected_plan_sha256")
        return submit(
            service, payload["specification"],
            expected_plan_sha256=payload["expected_plan_sha256"],
        )
