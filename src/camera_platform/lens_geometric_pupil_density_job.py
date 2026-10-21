"""Durable, revision-pinned geometric pupil-density sensitivity study.

The numerical solver is deliberately separate from this lifecycle adapter.
Submission hashes bounded Lens DB sources without decoding or tracing a PSF;
the Worker rechecks those identities around the isolated ray-density run.
"""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from typing import Any

from .registry import ConflictError, IntegrityError, canonical, digest

SCHEMA = "camerae2e_pinned_geometric_pupil_density_study_v2"
LEGACY_SCHEMA = "camerae2e_pinned_geometric_pupil_density_study_v1"
JOB_KIND = "lens_geometric_pupil_density"
WORKER_TIMEOUT_S = 420
ARTIFACT_NAME = "geometric-pupil-density-study.json"
LIMITATION = (
    "Dense-reference stability and pinned-source pupil ray-density adequacy are separate "
    "decisions for one exact stored field, wavelength and declared discrete spatial "
    "frequencies. The saved source pupil must reproduce its PSF node before denser pupil "
    "grids are assessed. Source ray-density adequacy does not qualify the saved PSF "
    "histogram pitch/quantization or execution-time PSF interpolation. Diffraction, "
    "pixel aperture, spectral/field coverage, physical lens accuracy, camera MTF and "
    "product ranking are not qualified. Ray and time budgets are policies; the soft "
    "solver deadline is backed by the Worker child-process timeout, not a hardware "
    "memory bound."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "고정 렌즈 PSF 동공 ray 밀도 진단",
        "lens_revision_id": "",
        "field_index": 0,
        "wavelength_nm": 550.0,
        "higher_pupil_samples": [81, 97, 113],
        "target_frequency_cycles_per_mm_row_col": [
            [100.0, 250.0, 400.0], [100.0, 250.0, 400.0],
        ],
        "maximum_complex_otf_delta": 0.01,
        "maximum_admitted_fraction_delta": 0.02,
        "max_total_requested_pupil_rays": 30_000,
        "soft_timeout_s": 300.0,
    }


def _number(value: Any, label: str, *, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a finite numeric value.")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{label} must be within [{minimum}, {maximum}].")
    return number


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the exact public contract before any catalog work."""
    template = default_spec()
    if not isinstance(payload, dict) or set(payload) != set(template):
        raise ValueError("Declare the exact geometric pupil-density study fields.")
    if payload["schema"] != SCHEMA:
        raise ValueError("Unsupported geometric pupil-density study schema.")
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Name requires 1..160 characters.")
    revision_id = payload["lens_revision_id"]
    if not isinstance(revision_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", revision_id):
        raise ValueError("Choose a Lens DB revision UUID, never a local path.")
    field = payload["field_index"]
    if type(field) is not int or not 0 <= field <= 6:
        raise ValueError("Choose one stored field index within [0, 6].")
    wave = _number(payload["wavelength_nm"], "Wavelength nm", minimum=100.0, maximum=2000.0)
    levels = payload["higher_pupil_samples"]
    if (
        not isinstance(levels, list) or not 2 <= len(levels) <= 4
        or any(type(level) is not int or not 3 <= level <= 257 for level in levels)
        or any(right <= left for left, right in zip(levels, levels[1:], strict=False))
    ):
        raise ValueError("Declare two to four increasing pupil-sample levels within [3, 257].")
    frequency_axes = payload["target_frequency_cycles_per_mm_row_col"]
    if not isinstance(frequency_axes, list) or len(frequency_axes) != 2:
        raise ValueError("Declare row and column target-frequency lists.")
    normalized_axes = []
    for axis in frequency_axes:
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
            raise ValueError("Target frequencies must increase within (0, 5000] cycles/mm.")
        normalized_axes.append(values)
    otf = _number(
        payload["maximum_complex_otf_delta"], "Complex OTF tolerance",
        minimum=1e-6, maximum=0.1,
    )
    admitted = _number(
        payload["maximum_admitted_fraction_delta"], "Admitted fraction tolerance",
        minimum=1e-6, maximum=0.1,
    )
    rays = payload["max_total_requested_pupil_rays"]
    if type(rays) is not int or not 1 <= rays <= 50_000:
        raise ValueError("Ray budget must be an integer within [1, 50000].")
    deadline = _number(payload["soft_timeout_s"], "Soft timeout seconds", minimum=1, maximum=300)
    result = deepcopy(payload)
    result.update(
        name=name.strip(), lens_revision_id=revision_id.lower(), wavelength_nm=wave,
        target_frequency_cycles_per_mm_row_col=normalized_axes,
        maximum_complex_otf_delta=otf,
        maximum_admitted_fraction_delta=admitted,
        soft_timeout_s=deadline,
    )
    return result


def _pin_lens_source(service: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Hash catalog, source DB and PSF asset; never decode PSF or trace rays."""
    from .lens_catalog_bridge import _asset, _revision_source
    from .lens_geometric_psf_rehistogram import _candidate
    from .lens_sensor_out_of_core_raw_job import _db_root
    from .lens_sensor_registered_roi_disk_raw_job import _bounded_file_sha256

    root = _db_root(service)
    revision, snapshot, db_path, db_sha = _revision_source(spec["lens_revision_id"], root)
    simulation_id, _asset_path, validation, artifact = _asset(
        revision, snapshot, db_path, root
    )
    candidate = _candidate(db_path, simulation_id)
    reviewed_root = db_path.parent / "reviewed_prescriptions"
    reviewed_path = reviewed_root / candidate.source_surfaces_csv
    if (
        reviewed_root.is_symlink() or reviewed_path.is_symlink()
        or not reviewed_path.resolve().is_relative_to(reviewed_root.resolve())
    ):
        raise ValueError("Pinned reviewed prescription path is unavailable or unsafe.")
    reviewed_sha = _bounded_file_sha256(
        reviewed_path, maximum_bytes=16 * 1024**2, label="reviewed prescription"
    )
    return {
        "lens_revision_id": spec["lens_revision_id"],
        "lens_revision_sha256": revision["content_sha256"],
        "lens_db_sha256": db_sha,
        "simulation_id": simulation_id,
        "source_psf_asset_sha256": validation["asset_sha256"],
        "source_prescription_sha256": validation["source_prescription_sha256"],
        "reviewed_prescription_file_sha256": reviewed_sha,
        "catalog_artifact_id": artifact["artifact_id"],
    }


def _prepare_pending(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Cheap, source-pinned queue document; exact field support is Worker-checked."""
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
        "submission_preflight_status": "exact_node_and_ray_budget_worker_preflight_pending",
        "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = _prepare_pending(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def _diagnostic_matches(
    diagnostic: dict[str, Any], spec: dict[str, Any], pin: dict[str, Any]
) -> bool:
    if not isinstance(diagnostic, dict):
        return False
    stable_status = "dense_geometric_pupil_reference_stable_at_declared_targets_model_only"
    supported_statuses = {
        stable_status,
        "inconclusive_pupil_ray_budget", "inconclusive_soft_timeout",
        "inconclusive_refinement_limit", "inconclusive_fixed_physical_support",
    }
    if diagnostic.get("status") not in supported_statuses:
        return False
    expected = {
        "schema": "camera_pinned_geometric_pupil_density_diagnostic_v2",
        "lens_revision_id": spec["lens_revision_id"],
        "simulation_id": pin["simulation_id"],
        "lens_db_sha256": pin["lens_db_sha256"],
        "source_psf_asset_sha256": pin["source_psf_asset_sha256"],
        "source_prescription_sha256": pin["source_prescription_sha256"],
        "field_index": spec["field_index"],
        "wavelength_nm": spec["wavelength_nm"],
        "higher_pupil_samples": spec["higher_pupil_samples"],
        "target_frequency_cycles_per_mm_row_col": (
            spec["target_frequency_cycles_per_mm_row_col"]
        ),
        "max_total_requested_pupil_rays": spec["max_total_requested_pupil_rays"],
        "maximum_complex_otf_delta_allowed": spec["maximum_complex_otf_delta"],
        "maximum_admitted_fraction_delta_allowed": spec[
            "maximum_admitted_fraction_delta"
        ],
        "soft_timeout_s": spec["soft_timeout_s"],
        "required_consecutive_refinement_passes": 2,
        "dense_refinement_baseline": "first_higher_pupil_grid",
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
        "diffraction_included": False,
        "pixel_aperture_included": False,
        "psf_histogram_interpolation_used_for_otf": False,
        "saved_psf_histogram_sampling_qualified": False,
    }
    if not all(diagnostic.get(key) == value for key, value in expected.items()):
        return False
    steps = diagnostic.get("steps")
    if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
        return False
    if diagnostic["status"] != stable_status:
        return (
            diagnostic.get("pinned_source_pupil_density_adequacy_status")
            == "not_evaluated_without_stable_dense_reference"
            and diagnostic.get("pinned_source_pupil_density_adequate_at_declared_targets") is None
            and diagnostic.get("pinned_source_pupil_vs_stable_dense_ray_reference") is None
        )
    if (
        len(steps) < 4 or len(steps) > len(spec["higher_pupil_samples"]) + 1
        or steps[0].get("source_histogram_reproduced") is not True
        or any(
            step.get("pupil_samples") != sample
            for step, sample in zip(
                steps[1:], spec["higher_pupil_samples"], strict=False
            )
        )
        or [(step.get("comparison_role"), step.get("consecutive_passes"))
            for step in steps[-2:]] != [("dense_refinement", 1), ("dense_refinement", 2)]
        or any(step.get("declared_numeric_tolerances_met") is not True
               for step in steps[-2:])
    ):
        return False

    def axis_delta(left: dict[str, Any], right: dict[str, Any], axis: str) -> float | None:
        key = f"complex_otf_{axis}"
        count = len(spec["target_frequency_cycles_per_mm_row_col"][
            0 if axis == "row_y_up" else 1
        ])
        pairs = (left.get(key), right.get(key))
        if any(not isinstance(values, list) or len(values) != count for values in pairs):
            return None
        delta = 0.0
        for a, b in zip(*pairs, strict=True):
            if (
                not isinstance(a, list) or not isinstance(b, list)
                or len(a) != 2 or len(b) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value)
                       for value in (*a, *b))
            ):
                return None
            delta = max(delta, abs(complex(*a) - complex(*b)))
        return delta

    def close(actual: Any, calculated: float) -> bool:
        return (
            type(actual) in (int, float) and math.isfinite(actual)
            and math.isclose(actual, calculated, rel_tol=1e-9, abs_tol=1e-12)
        )

    for left, right in ((steps[-3], steps[-2]), (steps[-2], steps[-1])):
        row = axis_delta(left, right, "row_y_up")
        col = axis_delta(left, right, "col_x_right")
        admitted = right.get("admitted_pupil_fraction")
        previous_admitted = left.get("admitted_pupil_fraction")
        if (
            row is None or col is None
            or type(admitted) not in (int, float)
            or type(previous_admitted) not in (int, float)
            or not 0 <= admitted <= 1 or not 0 <= previous_admitted <= 1
            or not close(right.get("maximum_complex_otf_delta_from_previous"), max(row, col))
            or not close(
                right.get("admitted_pupil_fraction_delta_from_previous"),
                abs(admitted - previous_admitted),
            )
            or max(row, col) > spec["maximum_complex_otf_delta"]
            or abs(admitted - previous_admitted) > spec["maximum_admitted_fraction_delta"]
        ):
            return False
    source, dense = steps[0], steps[-1]
    row = axis_delta(source, dense, "row_y_up")
    col = axis_delta(source, dense, "col_x_right")
    source_admitted = source.get("admitted_pupil_fraction")
    dense_admitted = dense.get("admitted_pupil_fraction")
    comparison = diagnostic.get("pinned_source_pupil_vs_stable_dense_ray_reference")
    if (
        row is None or col is None or not isinstance(comparison, dict)
        or type(source_admitted) not in (int, float)
        or type(dense_admitted) not in (int, float)
        or not 0 <= source_admitted <= 1 or not 0 <= dense_admitted <= 1
        or comparison.get("source_pupil_samples") != source.get("pupil_samples")
        or comparison.get("dense_reference_pupil_samples") != dense.get("pupil_samples")
        or not close(comparison.get("maximum_complex_otf_delta_row_y_up"), row)
        or not close(comparison.get("maximum_complex_otf_delta_col_x_right"), col)
        or not close(comparison.get("maximum_complex_otf_delta"), max(row, col))
        or not close(
            comparison.get("admitted_pupil_fraction_delta"),
            abs(source_admitted - dense_admitted),
        )
    ):
        return False
    adequate = (
        max(row, col) <= spec["maximum_complex_otf_delta"]
        and abs(source_admitted - dense_admitted)
        <= spec["maximum_admitted_fraction_delta"]
    )
    return (
        comparison.get("declared_numeric_tolerances_met") is adequate
        and diagnostic.get("pinned_source_pupil_density_adequate_at_declared_targets")
        is adequate
        and diagnostic.get("pinned_source_pupil_density_adequacy_status") == (
            "adequate_at_declared_targets_geometric_model_only" if adequate
            else "inadequate_at_declared_targets_geometric_model_only"
        )
    )


def _legacy_v1_diagnostic_matches(
    diagnostic: Any, spec: dict[str, Any], pin: dict[str, Any],
) -> bool:
    """Keep a completed v1 record readable without assigning it a v2 adequacy claim."""
    if not isinstance(diagnostic, dict):
        return False
    supported_statuses = {
        "geometric_pupil_density_stable_at_declared_targets_model_only",
        "inconclusive_pupil_ray_budget", "inconclusive_soft_timeout",
        "inconclusive_refinement_limit", "inconclusive_fixed_physical_support",
    }
    expected = {
        "schema": "camera_pinned_geometric_pupil_density_diagnostic_v1",
        "lens_revision_id": spec["lens_revision_id"],
        "simulation_id": pin["simulation_id"],
        "lens_db_sha256": pin["lens_db_sha256"],
        "source_psf_asset_sha256": pin["source_psf_asset_sha256"],
        "source_prescription_sha256": pin["source_prescription_sha256"],
        "field_index": spec["field_index"],
        "wavelength_nm": spec["wavelength_nm"],
        "higher_pupil_samples": spec["higher_pupil_samples"],
        "target_frequency_cycles_per_mm_row_col": (
            spec["target_frequency_cycles_per_mm_row_col"]
        ),
        "max_total_requested_pupil_rays": spec["max_total_requested_pupil_rays"],
        "maximum_complex_otf_delta_allowed": spec["maximum_complex_otf_delta"],
        "maximum_admitted_fraction_delta_allowed": spec["maximum_admitted_fraction_delta"],
        "soft_timeout_s": spec["soft_timeout_s"],
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
        "diffraction_included": False,
        "pixel_aperture_included": False,
        "psf_histogram_interpolation_used_for_otf": False,
    }
    if diagnostic.get("status") not in supported_statuses or not all(
        diagnostic.get(key) == value for key, value in expected.items()
    ):
        return False
    if diagnostic["status"] == "geometric_pupil_density_stable_at_declared_targets_model_only":
        steps = diagnostic.get("steps")
        return (
            isinstance(steps, list) and len(steps) >= 3
            and isinstance(steps[0], dict) and isinstance(steps[-1], dict)
            and steps[0].get("source_histogram_reproduced") is True
            and steps[-1].get("consecutive_passes", 0) >= 2
        )
    return True


def _legacy_v1_result_matches(job: dict[str, Any], summary: Any) -> bool:
    spec = job.get("specification")
    if not isinstance(spec, dict) or spec.get("schema") != LEGACY_SCHEMA:
        return False
    try:
        upgraded = specification({**spec, "schema": SCHEMA})
    except (TypeError, ValueError):
        return False
    if canonical({**upgraded, "schema": LEGACY_SCHEMA}) != canonical(spec):
        return False
    pin = job.get("pinned_source_binding")
    return (
        isinstance(summary, dict) and isinstance(pin, dict)
        and summary.get("schema") == LEGACY_SCHEMA
        and summary.get("status") == (summary.get("diagnostic") or {}).get("status")
        and summary.get("frozen_submission_sha256") == job.get("content_hash")
        and canonical(summary.get("specification")) == canonical(spec)
        and summary.get("pinned_source_binding") == pin
        and _legacy_v1_diagnostic_matches(summary.get("diagnostic"), spec, pin)
        and summary.get("physical_mtf_comparison_allowed") is False
        and summary.get("product_ranking_allowed") is False
        and summary.get("physical_accuracy_qualified") is False
    )


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .lens_geometric_pupil_density_diagnostic import (
        diagnose_pinned_geometric_pupil_density,
    )
    from .lens_sensor_out_of_core_raw_job import _db_root

    if (
        job.get("job_kind") != JOB_KIND
        or service.registry.get_run(job["id"])["status"] != "running"
    ):
        raise IntegrityError("Geometric pupil-density Worker received a non-running job.")
    spec = specification(job["specification"])
    if (
        canonical(spec) != canonical(job["specification"])
        or canonical(_prepare_pending(service, spec)) != canonical({
            key: job.get(key) for key in (
                "name", "job_kind", "plugin_id", "specification",
                "pinned_source_binding", "environment", "submission_preflight_status",
                "default_worker_timeout_seconds", "limitation",
            )
        })
        or service.loaded_environment != job.get("environment")
    ):
        raise IntegrityError("Pinned geometric pupil-density request changed before execution.")
    diagnostic = diagnose_pinned_geometric_pupil_density(
        spec["lens_revision_id"], catalog_root=_db_root(service),
        field_index=spec["field_index"], wavelength_nm=spec["wavelength_nm"],
        higher_pupil_samples=spec["higher_pupil_samples"],
        target_frequency_cycles_per_mm_row_col=(
            spec["target_frequency_cycles_per_mm_row_col"]
        ),
        maximum_complex_otf_delta=spec["maximum_complex_otf_delta"],
        maximum_admitted_fraction_delta=spec["maximum_admitted_fraction_delta"],
        max_total_requested_pupil_rays=spec["max_total_requested_pupil_rays"],
        soft_timeout_s=spec["soft_timeout_s"],
    )
    if not _diagnostic_matches(diagnostic, spec, job["pinned_source_binding"]):
        raise IntegrityError("Geometric pupil-density diagnostic differs from pinned inputs.")
    if (
        canonical(_prepare_pending(service, spec)) != canonical({
            key: job.get(key) for key in (
                "name", "job_kind", "plugin_id", "specification",
                "pinned_source_binding", "environment", "submission_preflight_status",
                "default_worker_timeout_seconds", "limitation",
            )
        })
        or service.registry.get_run(job["id"])["status"] != "running"
    ):
        raise IntegrityError("Pinned geometric pupil-density inputs changed before publication.")
    summary = {
        "schema": SCHEMA,
        "status": diagnostic["status"],
        "pinned_source_pupil_density_adequacy_status": diagnostic[
            "pinned_source_pupil_density_adequacy_status"
        ],
        "pinned_source_pupil_density_adequate_at_declared_targets": diagnostic[
            "pinned_source_pupil_density_adequate_at_declared_targets"
        ],
        "specification": spec,
        "frozen_submission_sha256": job["content_hash"],
        "pinned_source_binding": job["pinned_source_binding"],
        "diagnostic": diagnostic,
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
        "physical_accuracy_qualified": False,
        "limitation": LIMITATION,
    }
    encoded = canonical(summary).encode("utf-8")
    if len(encoded) > 2 * 1024**2:
        raise IntegrityError("Geometric pupil-density summary exceeds artifact size policy.")
    artifact = service.registry.store_blob(encoded, ARTIFACT_NAME, "application/json")
    return {"summary": summary, "artifacts": [artifact]}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a geometric pupil-density study.")
    summary = (job.get("result") or {}).get("summary")
    if (job["status"] == "succeeded") != (summary is not None):
        raise IntegrityError("Geometric pupil-density terminal status differs from result.")
    if summary is not None:
        if job.get("specification", {}).get("schema") == LEGACY_SCHEMA:
            if not _legacy_v1_result_matches(job, summary):
                raise IntegrityError("Legacy geometric pupil-density result differs from request.")
        else:
            spec = specification(job["specification"])
            pin = job["pinned_source_binding"]
            if (
                not isinstance(summary, dict)
                or summary.get("schema") != SCHEMA
                or summary.get("status") != (summary.get("diagnostic") or {}).get("status")
                or summary.get("pinned_source_pupil_density_adequacy_status") != (
                    summary.get("diagnostic") or {}
                ).get("pinned_source_pupil_density_adequacy_status")
                or summary.get("pinned_source_pupil_density_adequate_at_declared_targets") is not (
                    summary.get("diagnostic") or {}
                ).get("pinned_source_pupil_density_adequate_at_declared_targets")
                or summary.get("frozen_submission_sha256") != job["content_hash"]
                or canonical(summary.get("specification")) != canonical(spec)
                or summary.get("pinned_source_binding") != pin
                or not _diagnostic_matches(summary.get("diagnostic"), spec, pin)
                or summary.get("physical_mtf_comparison_allowed") is not False
                or summary.get("product_ranking_allowed") is not False
                or summary.get("physical_accuracy_qualified") is not False
            ):
                raise IntegrityError("Geometric pupil-density result differs from frozen request.")
        artifacts = (job.get("result") or {}).get("artifacts") or []
        if len(artifacts) != 1 or artifacts[0].get("name") != ARTIFACT_NAME:
            raise IntegrityError("Geometric pupil-density result artifact is missing.")
        path = service.registry.blob_path(artifacts[0]["sha256"])
        if path.stat().st_size > 2 * 1024**2:
            raise IntegrityError("Geometric pupil-density artifact exceeds size policy.")
        if json.loads(path.read_bytes()) != summary:
            raise IntegrityError("Geometric pupil-density artifact differs from summary.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/lens-geometric-pupil-density/template", tags=["lens_sampling"])
    def template():
        return {"specification": default_spec(), "limitation": LIMITATION}

    @app.post(
        "/api/v1/lens-geometric-pupil-density", status_code=202, tags=["lens_sampling"],
    )
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-geometric-pupil-density", tags=["lens_sampling"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-geometric-pupil-density/{identity}", tags=["lens_sampling"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/lens-geometric-pupil-density/{identity}/cancel", tags=["lens_sampling"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-geometric-pupil-density/{identity}/export", tags=["lens_sampling"])
    def export(identity: str):
        return Response(
            canonical(get(service, identity)), media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="geometric-pupil-density-study.json"'
            },
        )
