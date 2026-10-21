"""Owner-bound q4 rebin Job from a verified dense geometric q2 candidate."""

from __future__ import annotations

import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any

from .lens_dense_geometric_psf_candidate_job import _pin_lens_source
from .registry import ConflictError, IntegrityError, canonical, digest

SCHEMA = "camerae2e_pinned_dense_geometric_psf_q4_rebin_job_v1"
JOB_KIND = "lens_dense_geometric_psf_q4_rebin"
WORKER_TIMEOUT_S = 600
NPZ_NAME = "dense-geometric-psf-q4-rebin.npz"
MANIFEST_NAME = "dense-geometric-psf-q4-rebin.json"
MAX_MANIFEST_BYTES = 2 * 1024**2
MAX_NPZ_BYTES = 384 * 1024**2
LIMITATION = (
    "This reuses the exact admitted rays in one verified, owner-bound q2 candidate "
    "and changes only the geometric PSF histogram pitch to one quarter of the "
    "source. A declared target-frequency gap can pass or remain inconclusive; "
    "the artifact never promotes a Lens revision or qualifies pupil density, "
    "diffraction, pixel aperture, executed convolution, camera/system MTF, "
    "physical accuracy, or product ranking. Memory and time bounds are policies."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "동일 광선 기하 PSF 4배 세밀 격자",
        "parent_candidate_run_id": "",
        "parent_candidate_asset_sha256": "",
        "lens_revision_id": "",
        "source_psf_asset_sha256": "",
        "max_output_psf_bytes": 256 * 1024**2,
        "max_estimated_working_array_bytes": 512 * 1024**2,
        "soft_timeout_s": 300.0,
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare the exact q4 geometric rebin fields.")
    if payload["schema"] != SCHEMA:
        raise ValueError("Unsupported q4 geometric rebin schema.")
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("q4 name requires 1..160 characters.")
    run_id = payload["parent_candidate_run_id"]
    if not isinstance(run_id, str) or re.fullmatch(r"run-[0-9a-f]{32}", run_id) is None:
        raise ValueError("Choose one exact parent q2 candidate Run ID.")
    for key in ("parent_candidate_asset_sha256", "source_psf_asset_sha256"):
        value = payload[key]
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"Choose an exact 64-character {key}.")
    revision = payload["lens_revision_id"]
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-fA-F-]{36}", revision) is None:
        raise ValueError("Choose one Lens DB revision UUID, never a path.")
    output = payload["max_output_psf_bytes"]
    working = payload["max_estimated_working_array_bytes"]
    timeout = payload["soft_timeout_s"]
    if type(output) is not int or not 1 <= output <= 256 * 1024**2:
        raise ValueError("q4 output PSF byte policy must be within 256 MiB.")
    if type(working) is not int or not 1 <= working <= 512 * 1024**2:
        raise ValueError("q4 estimated working-array policy must be within 512 MiB.")
    if (
        type(timeout) not in (int, float) or not math.isfinite(float(timeout))
        or not 0 < float(timeout) <= 300
    ):
        raise ValueError("q4 soft timeout must be positive and at most 300 seconds.")
    return {
        **payload,
        "name": name.strip(),
        "lens_revision_id": revision.lower(),
        "soft_timeout_s": float(timeout),
    }


def _parent_binding(service: Any, spec: dict[str, Any]) -> dict[str, Any]:
    from .lens_dense_geometric_psf_candidate_v2_job import get as get_parent

    parent = get_parent(service, spec["parent_candidate_run_id"])
    if parent["status"] != "succeeded":
        raise ValueError("Parent q2 candidate Run must have succeeded.")
    summary = parent["result"]["summary"]
    manifest = summary["candidate_manifest"]
    archive = summary["candidate_archive_artifact"]
    manifest_artifact = summary["candidate_manifest_artifact"]
    current = _pin_lens_source(service, spec)
    if (
        archive["sha256"] != spec["parent_candidate_asset_sha256"]
        or manifest["candidate_asset_sha256"] != archive["sha256"]
        or manifest["lens_revision_id"] != spec["lens_revision_id"]
        or manifest["source_psf_asset_sha256"] != spec["source_psf_asset_sha256"]
        or manifest["lens_revision_content_sha256"] != current["lens_revision_sha256"]
        or manifest["lens_db_sha256"] != current["lens_db_sha256"]
        or manifest["source_prescription_sha256"] != current["source_prescription_sha256"]
        or manifest["source_psf_asset_sha256"] != current["source_psf_asset_sha256"]
        or manifest["simulation_id"] != current["simulation_id"]
    ):
        raise IntegrityError("Parent q2 Run, archive or current Lens source binding differs.")
    return {
        "parent_candidate_run_id": parent["id"],
        "parent_run_content_hash": parent["content_hash"],
        "parent_candidate_asset_sha256": archive["sha256"],
        "parent_candidate_manifest_artifact_sha256": manifest_artifact["sha256"],
        "parent_candidate_psf_array_sha256": manifest["candidate_psf_array_sha256"],
        "parent_dense_intersections_array_sha256": (
            manifest["dense_intersections_array_sha256"]
        ),
        "lens_revision_id": manifest["lens_revision_id"],
        "lens_revision_content_sha256": manifest["lens_revision_content_sha256"],
        "lens_db_sha256": manifest["lens_db_sha256"],
        "source_psf_asset_sha256": manifest["source_psf_asset_sha256"],
        "source_prescription_sha256": manifest["source_prescription_sha256"],
        "simulation_id": manifest["simulation_id"],
        "parent_source_histogram_pitch_mm_row_col": (
            manifest["source_histogram_pitch_mm_row_col"]
        ),
        "parent_q2_histogram_pitch_mm_row_col": (
            manifest["candidate_histogram_pitch_mm_row_col"]
        ),
        "source_psf_shape_row_col_field_wave": (
            manifest["source_psf_shape_row_col_field_wave"]
        ),
        "target_frequency_cycles_per_mm_row_col": (
            manifest["target_frequency_cycles_per_mm_row_col"]
        ),
        "maximum_histogram_complex_otf_abs_delta_allowed": (
            manifest["maximum_histogram_complex_otf_abs_delta_allowed"]
        ),
    }


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
        "pinned_parent_and_lens_binding": _parent_binding(service, spec),
        "environment": environment,
        "submission_preflight_status": "owner_bound_q2_ray_rebin_worker_preflight_pending",
        "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = _prepare_pending(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def _manifest_matches(manifest: Any, spec: dict[str, Any], binding: dict[str, Any]) -> bool:
    from .lens_dense_geometric_psf_q4_rebin import SCHEMA as PRODUCER_SCHEMA
    from .lens_dense_geometric_psf_q4_rebin import STATUS as PRODUCER_STATUS

    if not isinstance(manifest, dict):
        return False
    keys = (
        "parent_candidate_run_id", "parent_candidate_asset_sha256",
        "parent_candidate_psf_array_sha256", "parent_dense_intersections_array_sha256",
        "lens_revision_id", "lens_revision_content_sha256", "lens_db_sha256",
        "source_psf_asset_sha256", "source_prescription_sha256", "simulation_id",
        "source_psf_shape_row_col_field_wave", "target_frequency_cycles_per_mm_row_col",
        "maximum_histogram_complex_otf_abs_delta_allowed",
    )
    if any(manifest.get(key) != binding.get(key) for key in keys):
        return False
    source_shape = manifest.get("source_psf_shape_row_col_field_wave")
    target_shape = manifest.get("candidate_psf_shape_row_col_field_wave")
    source_pitch = manifest.get("source_histogram_pitch_mm_row_col")
    target_pitch = manifest.get("candidate_histogram_pitch_mm_row_col")
    fields, waves = manifest.get("field_height_mm"), manifest.get("wavelength_nm")
    nodes = manifest.get("node_reports")
    if (
        manifest.get("schema") != PRODUCER_SCHEMA
        or manifest.get("status") != PRODUCER_STATUS
        or manifest.get("all_stored_field_wavelength_nodes_completed") is not True
        or manifest.get("same_parent_dense_rays_reused_without_raytrace") is not True
        or manifest.get("histogram_refinement_factor") != 4
        or manifest.get("optical_psf_semantics") != "geometric_pre_pixel_ray_histogram"
        or manifest.get("diffraction_included") is not False
        or manifest.get("pixel_aperture_included") is not False
        or manifest.get("pupil_density_convergence_qualified_all_nodes") is not False
        or manifest.get("system_mtf_or_product_ranking_allowed") is not False
        or not isinstance(source_shape, list) or not isinstance(target_shape, list)
        or len(source_shape) != 4 or len(target_shape) != 4
        or target_shape[:2] != [4 * (side - 1) + 1 for side in source_shape[:2]]
        or source_shape[2:] != target_shape[2:]
        or not isinstance(source_pitch, list) or not isinstance(target_pitch, list)
        or len(source_pitch) != 2 or len(target_pitch) != 2
        or not all(type(value) in (int, float) and math.isfinite(float(value))
                   and value > 0 for value in (*source_pitch, *target_pitch))
        or not all(math.isclose(a / b, 4, rel_tol=0, abs_tol=1e-9)
                   for a, b in zip(source_pitch, target_pitch, strict=True))
        or source_pitch != binding["parent_source_histogram_pitch_mm_row_col"]
        or manifest.get("parent_q2_histogram_pitch_mm_row_col")
        != binding["parent_q2_histogram_pitch_mm_row_col"]
        or manifest.get("physical_sample_center_support_mm_row_col") != [
            (side - 1) * pitch for side, pitch in zip(
                source_shape[:2], source_pitch, strict=True,
            )
        ]
        or not isinstance(fields, list) or not isinstance(waves, list)
        or not isinstance(nodes, list)
        or target_shape[2:] != [len(fields), len(waves)]
        or manifest.get("node_count") != len(fields) * len(waves)
        or len(nodes) != manifest["node_count"]
        or manifest.get("output_psf_float32_bytes") != math.prod(target_shape) * 4
        or manifest["output_psf_float32_bytes"] > spec["max_output_psf_bytes"]
        or type(manifest.get("estimated_working_array_bytes_not_peak_rss")) is not int
        or manifest["estimated_working_array_bytes_not_peak_rss"]
        > spec["max_estimated_working_array_bytes"]
        or not isinstance(manifest.get("limits"), dict)
        or manifest["limits"].get("max_output_psf_bytes") != spec["max_output_psf_bytes"]
        or manifest["limits"].get("max_estimated_working_array_bytes")
        != spec["max_estimated_working_array_bytes"]
        or manifest["limits"].get("soft_timeout_s") != spec["soft_timeout_s"]
        or not isinstance(manifest.get("candidate_asset_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", manifest["candidate_asset_sha256"]) is None
        or manifest.get("candidate_asset_file")
        != f"{manifest['candidate_asset_sha256']}.npz"
        or not isinstance(manifest.get("candidate_psf_array_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", manifest["candidate_psf_array_sha256"]) is None
    ):
        return False
    expected = {(fi, wi) for fi in range(len(fields)) for wi in range(len(waves))}
    within = manifest.get("q4_worst_complex_otf_abs_gap", math.inf) <= (
        binding["maximum_histogram_complex_otf_abs_delta_allowed"]
    )
    return (
        all(isinstance(node, dict)
            and node.get("field_index") in range(len(fields))
            and node.get("wavelength_index") in range(len(waves))
            and node.get("field_height_mm") == fields[node["field_index"]]
            and node.get("wavelength_nm") == waves[node["wavelength_index"]]
            for node in nodes)
        and {(node["field_index"], node["wavelength_index"]) for node in nodes}
        == expected
        and manifest.get("histogram_gap_within_declared_tolerance_model_only") is within
        and manifest.get("histogram_target_status") == (
            "numeric_gap_within_declared_tolerance_model_only" if within
            else "inconclusive_target_histogram_gap"
        )
    )


def _frozen_request_matches(service: Any, job: dict[str, Any], spec: dict[str, Any]) -> bool:
    keys = (
        "name", "job_kind", "plugin_id", "specification", "pinned_parent_and_lens_binding",
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
    from .lens_dense_geometric_psf_candidate_v2_job import load_verified_candidate
    from .lens_dense_geometric_psf_q4_rebin import (
        build_q4_geometric_psf_rebin,
        load_q4_geometric_psf_rebin,
    )

    if (
        job.get("job_kind") != JOB_KIND
        or service.registry.get_run(job["id"])["status"] != "running"
    ):
        raise IntegrityError("q4 rebin Worker received a non-running job.")
    spec = specification(job["specification"])
    if not _frozen_request_matches(service, job, spec):
        raise IntegrityError("Pinned q4 parent or Lens source changed before execution.")
    binding = job["pinned_parent_and_lens_binding"]
    parent, arrays = load_verified_candidate(
        service, spec["parent_candidate_run_id"],
        candidate_asset_sha256=spec["parent_candidate_asset_sha256"],
        expected_lens_revision_id=spec["lens_revision_id"],
        expected_source_psf_asset_sha256=spec["source_psf_asset_sha256"],
    )
    if (
        parent["candidate_psf_array_sha256"]
        != binding["parent_candidate_psf_array_sha256"]
        or parent["dense_intersections_array_sha256"]
        != binding["parent_dense_intersections_array_sha256"]
    ):
        raise IntegrityError("Loaded q2 parent numerical arrays differ from submit pin.")
    with tempfile.TemporaryDirectory(
        prefix="dense-geometric-psf-q4-rebin-", dir=service.registry.directory,
    ) as directory:
        report = build_q4_geometric_psf_rebin(
            parent, arrays,
            parent_run_id=spec["parent_candidate_run_id"],
            parent_asset_sha256=spec["parent_candidate_asset_sha256"],
            output_dir=directory,
            max_output_psf_bytes=spec["max_output_psf_bytes"],
            max_estimated_working_array_bytes=(
                spec["max_estimated_working_array_bytes"]
            ),
            soft_timeout_s=spec["soft_timeout_s"],
        )
        root = Path(directory).resolve()
        archive_path = Path(report.get("candidate_asset_path", "")).resolve()
        report_path = Path(report.get("report_path", "")).resolve()
        if (
            archive_path.parent != root or report_path.parent != root
            or archive_path.is_symlink() or report_path.is_symlink()
            or not archive_path.is_file() or not report_path.is_file()
            or archive_path.stat().st_size > MAX_NPZ_BYTES
            or report_path.stat().st_size > MAX_MANIFEST_BYTES
        ):
            raise IntegrityError("q4 producer returned an unavailable or oversized asset.")
        recorded = json.loads(report_path.read_text(encoding="utf-8"))
        if (
            not _manifest_matches(recorded, spec, binding)
            or report.get("candidate_asset_sha256") != recorded["candidate_asset_sha256"]
            or archive_path.name != recorded["candidate_asset_file"]
        ):
            raise IntegrityError("q4 producer differs from pinned parent and Lens source.")
        load_q4_geometric_psf_rebin(
            report_path,
            expected_parent_run_id=spec["parent_candidate_run_id"],
            expected_parent_asset_sha256=spec["parent_candidate_asset_sha256"],
            expected_lens_revision_id=spec["lens_revision_id"],
            expected_source_psf_asset_sha256=spec["source_psf_asset_sha256"],
        )
        if not _frozen_request_matches(service, job, spec):
            raise IntegrityError("q4 parent or Lens source changed during rebinning.")
        archive = service.registry.store_file(archive_path, NPZ_NAME, "application/x-npz")
        if archive["sha256"] != recorded["candidate_asset_sha256"]:
            raise IntegrityError("Published q4 archive differs from producer hash.")
        public_manifest = dict(recorded)
        public_manifest.pop("candidate_asset_path", None)
        public_manifest.pop("report_path", None)
        if not _manifest_matches(public_manifest, spec, binding):
            raise IntegrityError("Path-free q4 manifest lost pinned lineage.")
        encoded = canonical(public_manifest).encode("utf-8")
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise IntegrityError("q4 manifest exceeds byte policy.")
        manifest_artifact = service.registry.store_blob(
            encoded, MANIFEST_NAME, "application/json",
        )
        if not _frozen_request_matches(service, job, spec):
            raise IntegrityError("q4 parent or Lens source changed before publication.")
        summary = {
            "schema": SCHEMA,
            "status": recorded["status"],
            "specification": spec,
            "frozen_submission_sha256": job["content_hash"],
            "pinned_parent_and_lens_binding": binding,
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
        raise ValueError("Selected job is not a q4 geometric PSF rebin.")
    summary = (job.get("result") or {}).get("summary")
    if (job["status"] == "succeeded") != (summary is not None):
        raise IntegrityError("q4 rebin terminal result differs from status.")
    if summary is not None:
        spec = specification(job["specification"])
        binding = job["pinned_parent_and_lens_binding"]
        manifest = summary.get("candidate_manifest")
        artifacts = (job.get("result") or {}).get("artifacts") or []
        archive = summary.get("candidate_archive_artifact")
        manifest_artifact = summary.get("candidate_manifest_artifact")
        if (
            summary.get("schema") != SCHEMA
            or summary.get("frozen_submission_sha256") != job["content_hash"]
            or canonical(summary.get("specification")) != canonical(spec)
            or summary.get("pinned_parent_and_lens_binding") != binding
            or not _manifest_matches(manifest, spec, binding)
            or "candidate_asset_path" in manifest or "report_path" in manifest
            or not isinstance(archive, dict) or not isinstance(manifest_artifact, dict)
            or archive.get("name") != NPZ_NAME
            or archive.get("sha256") != manifest["candidate_asset_sha256"]
            or manifest_artifact.get("name") != MANIFEST_NAME
            or artifacts != [archive, manifest_artifact]
            or summary.get("physical_mtf_comparison_allowed") is not False
            or summary.get("product_ranking_allowed") is not False
            or summary.get("physical_accuracy_qualified") is not False
        ):
            raise IntegrityError("q4 result differs from frozen lineage or claims.")
        archive_binding = service.registry.artifact_binding(
            archive["sha256"], owner_kind="runs", owner_id=identity,
        )
        manifest_binding = service.registry.artifact_binding(
            manifest_artifact["sha256"], owner_kind="runs", owner_id=identity,
        )
        if (
            archive_binding["descriptor"] != archive
            or manifest_binding["descriptor"] != manifest_artifact
            or archive_binding["path"].stat().st_size > MAX_NPZ_BYTES
            or manifest_binding["path"].stat().st_size > MAX_MANIFEST_BYTES
            or manifest_binding["path"].read_bytes() != canonical(manifest).encode("utf-8")
        ):
            raise IntegrityError("q4 artifact owner binding or manifest bytes differ.")
    return job


def load_verified_rebin(
    service: Any, run_id: str, *, candidate_asset_sha256: str,
    expected_parent_run_id: str, expected_parent_asset_sha256: str,
    expected_lens_revision_id: str, expected_source_psf_asset_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from .lens_dense_geometric_psf_candidate_v2_job import get as get_parent
    from .lens_dense_geometric_psf_q4_rebin import load_q4_geometric_psf_rebin

    if not isinstance(candidate_asset_sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", candidate_asset_sha256,
    ) is None:
        raise ValueError("Choose the exact q4 candidate archive SHA-256.")
    job = get(service, run_id)
    if job["status"] != "succeeded":
        raise ValueError("q4 rebin Run must have succeeded before use.")
    summary = job["result"]["summary"]
    binding = summary["pinned_parent_and_lens_binding"]
    archive = summary["candidate_archive_artifact"]
    manifest = summary["candidate_manifest_artifact"]
    if (
        archive["sha256"] != candidate_asset_sha256
        or binding["parent_candidate_run_id"] != expected_parent_run_id
        or binding["parent_candidate_asset_sha256"] != expected_parent_asset_sha256
        or binding["lens_revision_id"] != expected_lens_revision_id
        or binding["source_psf_asset_sha256"] != expected_source_psf_asset_sha256
    ):
        raise IntegrityError("q4 archive, parent or Lens identity differs.")
    parent = get_parent(service, expected_parent_run_id)
    parent_summary = (parent.get("result") or {}).get("summary")
    if (
        parent.get("status") != "succeeded"
        or parent_summary is None
        or parent_summary["candidate_archive_artifact"]["sha256"]
        != expected_parent_asset_sha256
        or parent_summary["candidate_manifest"]["candidate_psf_array_sha256"]
        != binding["parent_candidate_psf_array_sha256"]
        or parent_summary["candidate_manifest"]["dense_intersections_array_sha256"]
        != binding["parent_dense_intersections_array_sha256"]
    ):
        raise IntegrityError("q4 parent Run or admitted-ray lineage is no longer owner-bound.")
    archive_binding = service.registry.artifact_binding(
        archive["sha256"], owner_kind="runs", owner_id=run_id,
    )
    manifest_binding = service.registry.artifact_binding(
        manifest["sha256"], owner_kind="runs", owner_id=run_id,
    )
    loaded, arrays = load_q4_geometric_psf_rebin(
        manifest_binding["path"],
        expected_parent_run_id=expected_parent_run_id,
        expected_parent_asset_sha256=expected_parent_asset_sha256,
        expected_lens_revision_id=expected_lens_revision_id,
        expected_source_psf_asset_sha256=expected_source_psf_asset_sha256,
        candidate_asset_path=archive_binding["path"],
    )
    if loaded != summary["candidate_manifest"]:
        raise IntegrityError("q4 archive manifest differs from frozen Run result.")
    return loaded, arrays


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    route = "/api/v1/lens-dense-geometric-psf-q4-rebins"

    @app.get(route + "/template", tags=["lens_sampling"])
    def template():
        return {"specification": default_spec(), "limitation": LIMITATION}

    @app.post(route, status_code=202, tags=["lens_sampling"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get(route, tags=["lens_sampling"])
    def listing():
        return list_records(service)

    @app.get(route + "/{identity}", tags=["lens_sampling"])
    def detail(identity: str):
        return get(service, identity)

    @app.post(route + "/{identity}/cancel", tags=["lens_sampling"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get(route + "/{identity}/export", tags=["lens_sampling"])
    def export(identity: str):
        return Response(
            canonical(get(service, identity)), media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="dense-geometric-psf-q4-rebin.json"'
            },
        )
