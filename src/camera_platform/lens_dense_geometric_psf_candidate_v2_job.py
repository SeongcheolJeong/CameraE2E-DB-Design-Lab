"""Durable opt-in all-node dense geometric PSF candidate with half-pitch bins."""

from __future__ import annotations

import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any

from .lens_dense_geometric_psf_candidate_job import (
    _pin_lens_source,
)
from .lens_dense_geometric_psf_candidate_job import (
    specification as v1_specification,
)
from .registry import ConflictError, IntegrityError, canonical, digest

SCHEMA = "camerae2e_pinned_dense_geometric_psf_candidate_job_v2"
JOB_KIND = "lens_dense_geometric_psf_candidate_v2"
WORKER_TIMEOUT_S = 420
NPZ_NAME = "dense-geometric-psf-half-pitch-candidate.npz"
MANIFEST_NAME = "dense-geometric-psf-half-pitch-candidate.json"
MAX_MANIFEST_BYTES = 2 * 1024**2
MAX_NPZ_BYTES = 3 * 128 * 1024**2
LIMITATION = (
    "This is an opt-in, half-pitch geometric ray-histogram candidate on the exact "
    "source sample-center support. The same dense admitted rays are deposited at source "
    "and target pitch and compared with direct-ray complex OTF. A failed target gap is "
    "reported as inconclusive, never silently qualified. Even a passing gap does not "
    "establish pupil-density convergence, diffraction, pixel aperture, radiometry, "
    "Scene/OI sampling, system MTF, physical accuracy, or product ranking. Resource "
    "limits are policies, not hardware memory bounds."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "고밀도 기하 PSF 반 간격 후보 생성",
        "lens_revision_id": "",
        "pupil_samples": 145,
        "target_histogram_pitch_um": 0.0,
        "target_frequency_cycles_per_mm_row_col": [
            [100.0, 250.0, 400.0], [100.0, 250.0, 400.0],
        ],
        "maximum_histogram_complex_otf_abs_delta": 0.01,
        "max_total_requested_pupil_rays": 400_000,
        "max_output_psf_bytes": 128 * 1024**2,
        "soft_timeout_s": 300.0,
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept exact typed v2 controls; catalog and output paths remain server-owned."""
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare the exact fine geometric PSF candidate fields.")
    if payload["schema"] != SCHEMA:
        raise ValueError("Unsupported fine geometric PSF candidate schema.")
    pitch = payload["target_histogram_pitch_um"]
    tolerance = payload["maximum_histogram_complex_otf_abs_delta"]
    if (
        type(pitch) not in (int, float) or not math.isfinite(float(pitch))
        or not 0 < float(pitch) <= 100
    ):
        raise ValueError("Target histogram pitch must be finite and positive within 100 um.")
    if (
        type(tolerance) not in (int, float) or not math.isfinite(float(tolerance))
        or not 0 < float(tolerance) <= 0.1
    ):
        raise ValueError("Absolute complex-OTF tolerance must be within (0, 0.1].")
    old = {
        key: value for key, value in payload.items()
        if key not in (
            "target_histogram_pitch_um", "maximum_histogram_complex_otf_abs_delta",
        )
    }
    old["schema"] = "camerae2e_pinned_dense_geometric_psf_candidate_job_v1"
    normalized = v1_specification(old)
    normalized["schema"] = SCHEMA
    normalized["target_histogram_pitch_um"] = float(pitch)
    normalized["maximum_histogram_complex_otf_abs_delta"] = float(tolerance)
    return normalized


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
        "submission_preflight_status": "all_node_half_pitch_worker_preflight_pending",
        "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = _prepare_pending(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def _manifest_matches(manifest: Any, spec: dict[str, Any], pin: dict[str, Any]) -> bool:
    from .lens_dense_geometric_psf_candidate_v2 import SCHEMA as PRODUCER_SCHEMA
    from .lens_dense_geometric_psf_candidate_v2 import STATUS as PRODUCER_STATUS

    if not isinstance(manifest, dict):
        return False
    for source, pinned in (
        ("lens_revision_id", "lens_revision_id"),
        ("lens_revision_content_sha256", "lens_revision_sha256"),
        ("simulation_id", "simulation_id"),
        ("lens_db_sha256", "lens_db_sha256"),
        ("source_psf_asset_sha256", "source_psf_asset_sha256"),
        ("source_prescription_sha256", "source_prescription_sha256"),
    ):
        if manifest.get(source) != pin.get(pinned):
            return False
    source_shape = manifest.get("source_psf_shape_row_col_field_wave")
    target_shape = manifest.get("candidate_psf_shape_row_col_field_wave")
    source_pitch = manifest.get("source_histogram_pitch_mm_row_col")
    target_pitch = manifest.get("candidate_histogram_pitch_mm_row_col")
    fields, waves = manifest.get("field_height_mm"), manifest.get("wavelength_nm")
    nodes = manifest.get("node_reports")
    tolerance = spec["maximum_histogram_complex_otf_abs_delta"]
    if (
        manifest.get("schema") != PRODUCER_SCHEMA
        or manifest.get("status") != PRODUCER_STATUS
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
        or manifest.get("maximum_histogram_complex_otf_abs_delta_allowed") != tolerance
        or not isinstance(source_shape, list) or not isinstance(target_shape, list)
        or len(source_shape) != 4 or len(target_shape) != 4
        or source_shape[2:] != target_shape[2:]
        or target_shape[:2] != [2 * (side - 1) + 1 for side in source_shape[:2]]
        or manifest.get("histogram_refinement_factor") != 2
        or not isinstance(source_pitch, list) or not isinstance(target_pitch, list)
        or len(source_pitch) != 2 or len(target_pitch) != 2
        or not all(
            type(value) in (int, float) and math.isfinite(float(value)) and value > 0
            for value in (*source_pitch, *target_pitch)
        )
        or not all(math.isclose(a / b, 2, rel_tol=0, abs_tol=1e-9)
                   for a, b in zip(source_pitch, target_pitch, strict=True))
        or manifest.get("physical_sample_center_support_mm_row_col") != [
            (side - 1) * pitch
            for side, pitch in zip(source_shape[:2], source_pitch, strict=True)
        ]
        or not math.isclose(target_pitch[0] * 1000,
                            spec["target_histogram_pitch_um"], rel_tol=0, abs_tol=1e-12)
        or not isinstance(fields, list) or not isinstance(waves, list)
        or not isinstance(nodes, list)
        or target_shape[2:] != [len(fields), len(waves)]
        or len(nodes) != len(fields) * len(waves)
        or manifest.get("node_count") != len(nodes)
        or manifest.get("output_psf_float32_bytes") != math.prod(target_shape) * 4
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
        or re.fullmatch(r"[0-9a-f]{64}", manifest["candidate_asset_sha256"]) is None
        or manifest.get("candidate_asset_file")
        != f"{manifest['candidate_asset_sha256']}.npz"
        or not isinstance(manifest.get("candidate_psf_array_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", manifest["candidate_psf_array_sha256"]) is None
    ):
        return False
    expected = {(fi, wi) for fi in range(len(fields)) for wi in range(len(waves))}
    within = (
        manifest.get(
            "dense_target_pitch_binned_vs_direct_worst_complex_otf_abs_delta", math.inf,
        ) <= tolerance
    )
    return (
        all(isinstance(node, dict)
            and node.get("source_reproduced_before_dense_trace") is True
            and node.get("field_index") in range(len(fields))
            and node.get("wavelength_index") in range(len(waves))
            and node.get("field_height_mm") == fields[node["field_index"]]
            and node.get("wavelength_nm") == waves[node["wavelength_index"]]
            for node in nodes)
        and {(node["field_index"], node["wavelength_index"]) for node in nodes}
        == expected
        and manifest.get("target_histogram_gap_within_declared_tolerance_model_only")
        is within
        and manifest.get("histogram_target_status") == (
            "numeric_gap_within_declared_tolerance_model_only" if within
            else "inconclusive_target_histogram_gap"
        )
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
    """Generate, load-verify and publish a path-free content-addressed v2 pair."""
    from .lens_dense_geometric_psf_candidate_v2 import (
        build_pinned_dense_geometric_psf_candidate_v2,
        load_pinned_dense_geometric_psf_candidate_v2,
    )
    from .lens_sensor_out_of_core_raw_job import _db_root

    if (
        job.get("job_kind") != JOB_KIND
        or service.registry.get_run(job["id"])["status"] != "running"
    ):
        raise IntegrityError("Fine candidate Worker received a non-running job.")
    spec = specification(job["specification"])
    if not _frozen_request_matches(service, job, spec):
        raise IntegrityError("Pinned fine candidate request changed before execution.")
    pin = job["pinned_source_binding"]
    with tempfile.TemporaryDirectory(
        prefix="dense-geometric-psf-v2-", dir=service.registry.directory,
    ) as directory:
        report = build_pinned_dense_geometric_psf_candidate_v2(
            spec["lens_revision_id"], catalog_root=_db_root(service), output_dir=directory,
            pupil_samples=spec["pupil_samples"],
            target_histogram_pitch_um=spec["target_histogram_pitch_um"],
            target_frequency_cycles_per_mm_row_col=tuple(
                tuple(axis) for axis in spec["target_frequency_cycles_per_mm_row_col"]
            ),
            maximum_histogram_complex_otf_abs_delta=(
                spec["maximum_histogram_complex_otf_abs_delta"]
            ),
            max_total_requested_pupil_rays=spec["max_total_requested_pupil_rays"],
            max_output_psf_bytes=spec["max_output_psf_bytes"],
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
            raise IntegrityError("Producer returned an unavailable or unbounded private asset.")
        recorded = json.loads(report_path.read_text(encoding="utf-8"))
        if (
            not _manifest_matches(recorded, spec, pin)
            or report.get("candidate_asset_sha256") != recorded["candidate_asset_sha256"]
            or archive_path.name != recorded["candidate_asset_file"]
        ):
            raise IntegrityError("Fine candidate differs from pinned request.")
        load_pinned_dense_geometric_psf_candidate_v2(
            report_path, expected_lens_revision_id=pin["lens_revision_id"],
            expected_source_psf_asset_sha256=pin["source_psf_asset_sha256"],
        )
        if not _frozen_request_matches(service, job, spec):
            raise IntegrityError("Pinned fine candidate inputs changed during generation.")
        archive = service.registry.store_file(archive_path, NPZ_NAME, "application/x-npz")
        if archive["sha256"] != recorded["candidate_asset_sha256"]:
            raise IntegrityError("Published fine candidate differs from producer hash.")
        public_manifest = dict(recorded)
        public_manifest.pop("candidate_asset_path", None)
        public_manifest.pop("report_path", None)
        if not _manifest_matches(public_manifest, spec, pin):
            raise IntegrityError("Path-free fine manifest lost its source identity.")
        encoded = canonical(public_manifest).encode("utf-8")
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise IntegrityError("Fine candidate manifest exceeds size policy.")
        manifest_artifact = service.registry.store_blob(
            encoded, MANIFEST_NAME, "application/json",
        )
        if not _frozen_request_matches(service, job, spec):
            raise IntegrityError("Pinned fine candidate inputs changed before publication.")
        summary = {
            "schema": SCHEMA,
            "status": recorded["status"],
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
        raise ValueError("Selected job is not a fine geometric PSF candidate.")
    summary = (job.get("result") or {}).get("summary")
    if (job["status"] == "succeeded") != (summary is not None):
        raise IntegrityError("Fine candidate terminal result differs from status.")
    if summary is not None:
        spec = specification(job["specification"])
        pin = job["pinned_source_binding"]
        manifest = summary.get("candidate_manifest")
        artifacts = (job.get("result") or {}).get("artifacts") or []
        archive = summary.get("candidate_archive_artifact")
        manifest_artifact = summary.get("candidate_manifest_artifact")
        if (
            summary.get("schema") != SCHEMA
            or summary.get("frozen_submission_sha256") != job["content_hash"]
            or canonical(summary.get("specification")) != canonical(spec)
            or summary.get("pinned_source_binding") != pin
            or not _manifest_matches(manifest, spec, pin)
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
            raise IntegrityError("Fine candidate differs from frozen request.")
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
            raise IntegrityError("Fine candidate artifact binding differs.")
    return job


def load_verified_candidate(
    service: Any, candidate_run_id: str, *, candidate_asset_sha256: str,
    expected_lens_revision_id: str, expected_source_psf_asset_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load only an exact owner-bound v2 Run; the catalog Lens stays authoritative."""
    from .lens_dense_geometric_psf_candidate_v2 import (
        load_pinned_dense_geometric_psf_candidate_v2,
    )

    if not isinstance(candidate_asset_sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", candidate_asset_sha256,
    ) is None:
        raise ValueError("Choose the exact fine candidate archive SHA-256.")
    job = get(service, candidate_run_id)
    if job["status"] != "succeeded":
        raise ValueError("Fine candidate must have succeeded before use.")
    summary = job["result"]["summary"]
    pin = summary["pinned_source_binding"]
    if (
        candidate_asset_sha256 != summary["candidate_archive_artifact"]["sha256"]
        or candidate_asset_sha256 != summary["candidate_manifest"]["candidate_asset_sha256"]
        or expected_lens_revision_id != pin["lens_revision_id"]
        or expected_source_psf_asset_sha256 != pin["source_psf_asset_sha256"]
    ):
        raise IntegrityError("Fine candidate SHA or catalog Lens PSF binding differs.")
    archive = summary["candidate_archive_artifact"]
    manifest = summary["candidate_manifest_artifact"]
    archive_binding = service.registry.artifact_binding(
        archive["sha256"], owner_kind="runs", owner_id=candidate_run_id,
    )
    manifest_binding = service.registry.artifact_binding(
        manifest["sha256"], owner_kind="runs", owner_id=candidate_run_id,
    )
    loaded, arrays = load_pinned_dense_geometric_psf_candidate_v2(
        manifest_binding["path"],
        expected_lens_revision_id=expected_lens_revision_id,
        expected_source_psf_asset_sha256=expected_source_psf_asset_sha256,
        candidate_asset_path=archive_binding["path"],
    )
    if loaded != summary["candidate_manifest"]:
        raise IntegrityError("Fine candidate archive manifest differs from frozen Run result.")
    return loaded, arrays


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    route = "/api/v1/lens-dense-geometric-psf-candidates-v2"

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
                "Content-Disposition": (
                    'attachment; filename="dense-geometric-psf-candidate-v2.json"'
                ),
            },
        )
