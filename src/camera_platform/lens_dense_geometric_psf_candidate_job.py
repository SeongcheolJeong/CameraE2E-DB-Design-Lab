"""Durable, source-pinned generation of an opt-in dense geometric PSF candidate.

The candidate is a separate numerical response artifact, never a replacement
for the imported Lens revision or its catalog ``raytrace_psf_v2`` asset.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any

from .registry import ConflictError, IntegrityError, canonical, digest

SCHEMA = "camerae2e_pinned_dense_geometric_psf_candidate_job_v1"
JOB_KIND = "lens_dense_geometric_psf_candidate"
WORKER_TIMEOUT_S = 420
NPZ_NAME = "dense-geometric-psf-candidate.npz"
MANIFEST_NAME = "dense-geometric-psf-candidate.json"
MAX_MANIFEST_BYTES = 2 * 1024**2
MAX_NPZ_BYTES = 3 * 128 * 1024**2
LIMITATION = (
    "This opt-in artifact is a higher-pupil-density geometric ray histogram at the "
    "original fixed physical PSF pitch and support. All stored field and wavelength "
    "nodes must reproduce the pinned source before generation. Generation does not "
    "establish pupil-density or histogram-grid convergence, diffraction, pixel-aperture "
    "response, radiometry, physical lens accuracy, system MTF, or product ranking. "
    "Ray, array, and time limits are policies rather than hardware memory bounds."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "고밀도 기하 PSF 후보 생성",
        "lens_revision_id": "",
        "pupil_samples": 145,
        "target_frequency_cycles_per_mm_row_col": [
            [100.0, 250.0, 400.0, 500.0, 800.0],
            [100.0, 250.0, 400.0, 500.0, 800.0],
        ],
        "max_total_requested_pupil_rays": 400_000,
        "max_output_psf_bytes": 128 * 1024**2,
        "soft_timeout_s": 300.0,
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept exact typed controls; no path or caller-supplied asset is accepted."""
    template = default_spec()
    if not isinstance(payload, dict) or set(payload) != set(template):
        raise ValueError("Declare the exact dense geometric PSF candidate fields.")
    if payload["schema"] != SCHEMA:
        raise ValueError("Unsupported dense geometric PSF candidate schema.")
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Name requires 1..160 characters.")
    revision = payload["lens_revision_id"]
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", revision):
        raise ValueError("Choose one Lens DB revision UUID, never a local path.")
    samples = payload["pupil_samples"]
    if type(samples) is not int or not 66 <= samples <= 257:
        raise ValueError("Dense pupil samples must be an integer within [66, 257].")
    targets = payload["target_frequency_cycles_per_mm_row_col"]
    if not isinstance(targets, list) or len(targets) != 2:
        raise ValueError("Declare row and column frequency targets.")
    normalized_targets = []
    for axis in targets:
        if (
            not isinstance(axis, list) or not 1 <= len(axis) <= 64
            or any(type(value) not in (int, float) for value in axis)
        ):
            raise ValueError("Each frequency axis requires 1..64 numeric targets.")
        values = [float(value) for value in axis]
        if (
            not all(math.isfinite(value) and 0 < value <= 5000 for value in values)
            or any(right <= left for left, right in zip(values, values[1:], strict=False))
        ):
            raise ValueError("Frequency targets must increase within (0, 5000] cycles/mm.")
        normalized_targets.append(values)
    rays = payload["max_total_requested_pupil_rays"]
    if type(rays) is not int or not 1 <= rays <= 500_000:
        raise ValueError("Requested pupil-ray budget must be within [1, 500000].")
    psf_bytes = payload["max_output_psf_bytes"]
    if type(psf_bytes) is not int or not 1 <= psf_bytes <= 128 * 1024**2:
        raise ValueError("Output PSF budget must be within [1, 128 MiB].")
    timeout = payload["soft_timeout_s"]
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(float(timeout))
        or not 0 < timeout <= 300
    ):
        raise ValueError("Soft timeout must be positive and at most 300 seconds.")
    return {
        **payload,
        "name": name.strip(),
        "lens_revision_id": revision.lower(),
        "target_frequency_cycles_per_mm_row_col": normalized_targets,
        "soft_timeout_s": float(timeout),
    }


def _pin_lens_source(service: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Hash current revision/source/PSF/prescription without tracing pupil rays."""
    from .lens_geometric_pupil_density_job import _pin_lens_source as pin

    return pin(service, spec)


def _prepare_pending(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .service import environment_fingerprint

    spec = specification(payload)
    environment = environment_fingerprint()
    if service.loaded_environment != environment:
        raise ConflictError("Executable source changed after startup; restart the API.")
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "pinned_source_binding": _pin_lens_source(service, spec),
        "environment": environment,
        "submission_preflight_status": "all_node_raytrace_worker_preflight_pending",
        "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = _prepare_pending(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def _manifest_matches(manifest: dict[str, Any], spec: dict[str, Any], pin: dict[str, Any]) -> bool:
    from .lens_dense_geometric_psf_candidate import SCHEMA as PRODUCER_SCHEMA

    if not isinstance(manifest, dict):
        return False
    source_pairs = (
        ("lens_revision_id", "lens_revision_id"),
        ("lens_revision_content_sha256", "lens_revision_sha256"),
        ("simulation_id", "simulation_id"),
        ("lens_db_sha256", "lens_db_sha256"),
        ("source_psf_asset_sha256", "source_psf_asset_sha256"),
        ("source_prescription_sha256", "source_prescription_sha256"),
    )
    if any(manifest.get(source) != pin.get(pinned) for source, pinned in source_pairs):
        return False
    shape = manifest.get("candidate_psf_shape_row_col_field_wave")
    source_shape = manifest.get("source_psf_shape_row_col_field_wave")
    fields = manifest.get("field_height_mm")
    waves = manifest.get("wavelength_nm")
    nodes = manifest.get("node_reports")
    if (
        manifest.get("schema") != PRODUCER_SCHEMA
        or manifest.get("status") != "all_stored_nodes_candidate_generated_model_only"
        or manifest.get("all_stored_field_wavelength_nodes_completed") is not True
        or manifest.get("source_reproduction_completed_before_any_dense_trace") is not True
        or manifest.get("optical_psf_semantics") != "geometric_pre_pixel_ray_histogram"
        or manifest.get("diffraction_included") is not False
        or manifest.get("pixel_aperture_included") is not False
        or manifest.get("pupil_density_convergence_qualified_all_nodes") is not False
        or manifest.get("system_mtf_or_product_ranking_allowed") is not False
        or manifest.get("dense_pupil_samples") != spec["pupil_samples"]
        or manifest.get("target_frequency_cycles_per_mm_row_col")
        != spec["target_frequency_cycles_per_mm_row_col"]
        or not isinstance(fields, list) or not isinstance(waves, list)
        or not isinstance(nodes, list)
        or not isinstance(shape, list) or len(shape) != 4
        or shape != source_shape
        or shape[2:] != [len(fields), len(waves)]
        or manifest.get("node_count") != len(fields) * len(waves)
        or len(nodes) != manifest["node_count"]
        or manifest.get("output_psf_float32_bytes") != (
            math.prod(shape) * 4
        )
        or manifest["output_psf_float32_bytes"] > spec["max_output_psf_bytes"]
        or type(manifest.get("requested_pupil_rays")) is not int
        or not 0 < manifest["requested_pupil_rays"]
        <= spec["max_total_requested_pupil_rays"]
        or not isinstance(manifest.get("limits"), dict)
        or manifest["limits"].get("max_total_requested_pupil_rays")
        != spec["max_total_requested_pupil_rays"]
        or manifest["limits"].get("max_output_psf_bytes")
        != spec["max_output_psf_bytes"]
        or manifest["limits"].get("soft_timeout_s") != spec["soft_timeout_s"]
        or not isinstance(manifest.get("candidate_asset_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", manifest["candidate_asset_sha256"])
        or manifest.get("candidate_asset_file")
        != f"{manifest['candidate_asset_sha256']}.npz"
        or not isinstance(manifest.get("candidate_psf_array_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", manifest["candidate_psf_array_sha256"])
    ):
        return False
    expected_nodes = {(field, wave) for field in range(len(fields)) for wave in range(len(waves))}
    return (
        all(
            isinstance(node, dict)
            and node.get("source_reproduced_before_dense_trace") is True
            and node.get("field_index") in range(len(fields))
            and node.get("wavelength_index") in range(len(waves))
            and node.get("field_height_mm") == fields[node["field_index"]]
            and node.get("wavelength_nm") == waves[node["wavelength_index"]]
            for node in nodes
        )
        and {(node["field_index"], node["wavelength_index"]) for node in nodes}
        == expected_nodes
    )


def _frozen_request_matches(service: Any, job: dict[str, Any], spec: dict[str, Any]) -> bool:
    keys = (
        "name", "job_kind", "plugin_id", "specification", "pinned_source_binding",
        "environment", "submission_preflight_status", "default_worker_timeout_seconds",
        "limitation",
    )
    return (
        canonical(spec) == canonical(job.get("specification"))
        and canonical(_prepare_pending(service, spec))
        == canonical({key: job.get(key) for key in keys})
        and service.loaded_environment == job.get("environment")
    )


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    """Generate and publish a path-free, content-addressed candidate pair."""
    from .lens_dense_geometric_psf_candidate import (
        build_pinned_dense_geometric_psf_candidate,
        load_pinned_dense_geometric_psf_candidate,
    )
    from .lens_sensor_out_of_core_raw_job import _db_root

    if (
        job.get("job_kind") != JOB_KIND
        or service.registry.get_run(job["id"])["status"] != "running"
    ):
        raise IntegrityError("Dense geometric PSF Worker received a non-running job.")
    spec = specification(job["specification"])
    if not _frozen_request_matches(service, job, spec):
        raise IntegrityError("Pinned dense geometric PSF request changed before execution.")
    pin = job["pinned_source_binding"]
    with tempfile.TemporaryDirectory(
        prefix="dense-geometric-psf-", dir=service.registry.directory
    ) as directory:
        report = build_pinned_dense_geometric_psf_candidate(
            spec["lens_revision_id"], catalog_root=_db_root(service), output_dir=directory,
            pupil_samples=spec["pupil_samples"],
            target_frequency_cycles_per_mm_row_col=tuple(
                tuple(axis) for axis in spec["target_frequency_cycles_per_mm_row_col"]
            ),
            max_total_requested_pupil_rays=spec["max_total_requested_pupil_rays"],
            max_output_psf_bytes=spec["max_output_psf_bytes"],
            soft_timeout_s=spec["soft_timeout_s"],
        )
        root = Path(directory).resolve()
        asset_path = Path(report.get("candidate_asset_path", "")).resolve()
        report_path = Path(report.get("report_path", "")).resolve()
        if (
            asset_path.parent != root or report_path.parent != root
            or asset_path.is_symlink() or report_path.is_symlink()
            or not asset_path.is_file() or not report_path.is_file()
            or asset_path.stat().st_size > MAX_NPZ_BYTES
            or report_path.stat().st_size > MAX_MANIFEST_BYTES
        ):
            raise IntegrityError("Producer returned an unavailable or unbounded private asset.")
        recorded = json.loads(report_path.read_text(encoding="utf-8"))
        if (
            not _manifest_matches(recorded, spec, pin)
            or report.get("candidate_asset_sha256") != recorded["candidate_asset_sha256"]
            or asset_path.name != recorded["candidate_asset_file"]
        ):
            raise IntegrityError("Dense geometric candidate differs from pinned request.")
        # This verifies the archive arrays, fixed axes, embedded lineage and
        # actual archive bytes before they become public Registry artifacts.
        load_pinned_dense_geometric_psf_candidate(
            report_path,
            expected_lens_revision_id=pin["lens_revision_id"],
            expected_source_psf_asset_sha256=pin["source_psf_asset_sha256"],
        )
        if not _frozen_request_matches(service, job, spec):
            raise IntegrityError("Pinned dense geometric PSF inputs changed during generation.")
        archive = service.registry.store_file(asset_path, NPZ_NAME, "application/x-npz")
        if archive["sha256"] != recorded["candidate_asset_sha256"]:
            raise IntegrityError("Published candidate archive differs from producer hash.")
        public_manifest = dict(recorded)
        public_manifest.pop("candidate_asset_path", None)
        public_manifest.pop("report_path", None)
        if not _manifest_matches(public_manifest, spec, pin):
            raise IntegrityError("Path-free candidate manifest lost its source identity.")
        encoded = canonical(public_manifest).encode("utf-8")
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise IntegrityError("Dense geometric candidate manifest exceeds size policy.")
        manifest_artifact = service.registry.store_blob(
            encoded, MANIFEST_NAME, "application/json"
        )
        if not _frozen_request_matches(service, job, spec):
            raise IntegrityError("Pinned dense geometric PSF inputs changed before publication.")
        summary = {
            "schema": SCHEMA,
            "status": "all_stored_nodes_candidate_generated_model_only",
            "specification": spec,
            "frozen_submission_sha256": job["content_hash"],
            "pinned_source_binding": pin,
            "candidate_manifest": public_manifest,
            "candidate_archive_artifact": archive,
            "candidate_manifest_artifact": manifest_artifact,
            "physical_mtf_comparison_allowed": False,
            "product_ranking_allowed": False,
            "physical_accuracy_qualified": False,
            "limitation": LIMITATION,
        }
        return {"summary": summary, "artifacts": [archive, manifest_artifact]}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a dense geometric PSF candidate.")
    summary = (job.get("result") or {}).get("summary")
    if (job["status"] == "succeeded") != (summary is not None):
        raise IntegrityError("Dense geometric candidate terminal result differs from status.")
    if summary is not None:
        spec = specification(job["specification"])
        pin = job["pinned_source_binding"]
        manifest = summary.get("candidate_manifest")
        artifacts = (job.get("result") or {}).get("artifacts") or []
        archive = summary.get("candidate_archive_artifact")
        manifest_artifact = summary.get("candidate_manifest_artifact")
        if (
            summary.get("schema") != SCHEMA
            or summary.get("status") != "all_stored_nodes_candidate_generated_model_only"
            or canonical(summary.get("specification")) != canonical(spec)
            or summary.get("frozen_submission_sha256") != job["content_hash"]
            or summary.get("pinned_source_binding") != pin
            or not _manifest_matches(manifest, spec, pin)
            or isinstance(manifest, dict) and (
                "candidate_asset_path" in manifest or "report_path" in manifest
            )
            or not isinstance(archive, dict) or not isinstance(manifest_artifact, dict)
            or archive.get("name") != NPZ_NAME
            or archive.get("sha256") != manifest["candidate_asset_sha256"]
            or manifest_artifact.get("name") != MANIFEST_NAME
            or artifacts != [archive, manifest_artifact]
            or summary.get("physical_mtf_comparison_allowed") is not False
            or summary.get("product_ranking_allowed") is not False
            or summary.get("physical_accuracy_qualified") is not False
        ):
            raise IntegrityError("Dense geometric candidate differs from frozen request.")
        archive_binding = service.registry.artifact_binding(
            archive["sha256"], owner_kind="runs", owner_id=identity
        )
        manifest_binding = service.registry.artifact_binding(
            manifest_artifact["sha256"], owner_kind="runs", owner_id=identity
        )
        if (
            archive_binding["descriptor"] != archive
            or manifest_binding["descriptor"] != manifest_artifact
            or archive_binding["path"].stat().st_size > MAX_NPZ_BYTES
            or manifest_binding["path"].stat().st_size > MAX_MANIFEST_BYTES
            or manifest_binding["path"].read_bytes() != canonical(manifest).encode("utf-8")
        ):
            raise IntegrityError("Dense geometric candidate artifact binding differs.")
    return job


def load_verified_candidate(
    service: Any, candidate_run_id: str, *, candidate_asset_sha256: str,
    expected_lens_revision_id: str, expected_source_psf_asset_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Decode an exact owner-bound candidate for an explicitly opt-in consumer.

    A caller must first load its normal catalog Lens and pass that Lens's
    verified revision and original PSF SHA. The candidate has a separate
    artifact identity and must never silently replace the catalog response.
    """
    from .lens_dense_geometric_psf_candidate import load_pinned_dense_geometric_psf_candidate

    if not isinstance(candidate_asset_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", candidate_asset_sha256
    ):
        raise ValueError("Choose the exact 64-character candidate archive SHA-256.")
    job = get(service, candidate_run_id)
    if job["status"] != "succeeded":
        raise ValueError("Dense geometric PSF candidate must have succeeded before use.")
    summary = job["result"]["summary"]
    pin = summary["pinned_source_binding"]
    if (
        candidate_asset_sha256 != summary["candidate_archive_artifact"]["sha256"]
        or candidate_asset_sha256 != summary["candidate_manifest"]["candidate_asset_sha256"]
        or
        expected_lens_revision_id != pin["lens_revision_id"]
        or expected_source_psf_asset_sha256 != pin["source_psf_asset_sha256"]
    ):
        raise IntegrityError("Candidate SHA or catalog Lens PSF binding differs.")
    archive = summary["candidate_archive_artifact"]
    manifest = summary["candidate_manifest_artifact"]
    archive_binding = service.registry.artifact_binding(
        archive["sha256"], owner_kind="runs", owner_id=candidate_run_id
    )
    manifest_binding = service.registry.artifact_binding(
        manifest["sha256"], owner_kind="runs", owner_id=candidate_run_id
    )
    loaded, arrays = load_pinned_dense_geometric_psf_candidate(
        manifest_binding["path"],
        expected_lens_revision_id=expected_lens_revision_id,
        expected_source_psf_asset_sha256=expected_source_psf_asset_sha256,
        candidate_asset_path=archive_binding["path"],
    )
    if loaded != summary["candidate_manifest"]:
        raise IntegrityError("Candidate archive manifest differs from its frozen Run result.")
    return loaded, arrays


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs()
        if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/lens-dense-geometric-psf-candidates/template", tags=["lens_sampling"])
    def template():
        return {"specification": default_spec(), "limitation": LIMITATION}

    @app.post(
        "/api/v1/lens-dense-geometric-psf-candidates", status_code=202,
        tags=["lens_sampling"],
    )
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-dense-geometric-psf-candidates", tags=["lens_sampling"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-dense-geometric-psf-candidates/{identity}", tags=["lens_sampling"])
    def detail(identity: str):
        return get(service, identity)

    @app.post(
        "/api/v1/lens-dense-geometric-psf-candidates/{identity}/cancel",
        tags=["lens_sampling"],
    )
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get(
        "/api/v1/lens-dense-geometric-psf-candidates/{identity}/export",
        tags=["lens_sampling"],
    )
    def export(identity: str):
        return Response(
            canonical(get(service, identity)), media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="dense-geometric-psf-candidate.json"'
            },
        )
