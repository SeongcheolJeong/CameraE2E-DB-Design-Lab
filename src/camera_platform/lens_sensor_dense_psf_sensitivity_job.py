"""Durable fixed-grid Lens x Sensor sensitivity to an opt-in dense PSF Run.

The registered Lens/Sensor/active-mode authority and the candidate Run are
both frozen at submit.  The Worker rechecks them and compares only the PSF
planes on one identical Scene/OI grid.  This is not a PSF promotion or an MTF
qualification.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from typing import Any

from .lens_sensor_frequency_target_job import _fixed_tangent_plane_shift_deg
from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_dense_psf_fixed_grid_sensitivity_job_v1"
JOB_KIND = "lens_sensor_dense_psf_sensitivity"
WORKER_TIMEOUT_S = 1800
ARTIFACT_NAME = "dense-psf-fixed-grid-sensitivity.json"
LIMITATION = (
    "The same registered Lens/Sensor model, analytic Scene, field of view and one "
    "optical-image grid are used for source-versus-candidate geometric PSF shape "
    "sensitivity. The candidate is an opt-in higher-pupil-density numerical asset, "
    "not a qualified replacement. Source radiometry/direction and the sensor "
    "pixel operator are held fixed. Grid and pupil-density convergence, diffraction, "
    "physical lens MTF, product ranking and full-frame behavior are not established."
)


def _fixed_grid_shift_geometry(
    *, edge_offset_deg: float, native_pitch_um: float,
    requested_fraction: float, image_distance_m: float,
) -> dict[str, Any]:
    requested_shift_um = native_pitch_um * requested_fraction
    angular_shift_deg = _fixed_tangent_plane_shift_deg(
        edge_offset_deg=edge_offset_deg,
        requested_shift_um=requested_shift_um,
        image_distance_m=image_distance_m,
    )
    return {
        "requested_native_pixel_fraction": requested_fraction,
        "requested_tangent_plane_um": requested_shift_um,
        "planned_angular_shift_deg": angular_shift_deg,
        "distorted_local_edge_shift_qualified": False,
    }


def default_spec() -> dict[str, Any]:
    from .lens_sensor_registered_roi_disk_raw_job import default_spec as roi_default

    return {
        "schema": SCHEMA,
        "name": "렌즈×센서 고밀도 PSF 고정 격자 민감도",
        "registered_roi": roi_default(),
        "candidate_run_id": "",
        "candidate_asset_sha256": "",
        "fixed_grid_goal": {
            "edge_shift_native_pixel_fraction": 0.25,
            "target_fractions_native_nyquist": [0.25, 0.9],
            "optical_tile_estimated_working_budget_bytes": 512 * 1024**2,
        },
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    from .lens_sensor_registered_roi_disk_raw_job import specification as roi_spec

    template = default_spec()
    if not isinstance(payload, dict) or set(payload) != set(template):
        raise ValueError("Declare the exact dense-PSF fixed-grid job fields.")
    if payload["schema"] != SCHEMA:
        raise ValueError("Unsupported dense-PSF fixed-grid job schema.")
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Name requires 1..160 characters.")
    roi = roi_spec(payload["registered_roi"])
    if roi["capture"]["noise_flag"] != 0:
        raise ValueError("Fixed-grid PSF sensitivity requires noise_flag=0.")
    run_id = payload["candidate_run_id"]
    sha = payload["candidate_asset_sha256"]
    if not isinstance(run_id, str) or re.fullmatch(r"run-[0-9a-f]{32}", run_id) is None:
        raise ValueError("Choose one completed candidate Run ID.")
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
        raise ValueError("Choose the candidate NPZ artifact SHA-256.")
    goal = payload["fixed_grid_goal"]
    if not isinstance(goal, dict) or set(goal) != set(template["fixed_grid_goal"]):
        raise ValueError("Declare the exact fixed-grid goal fields.")
    shift = goal["edge_shift_native_pixel_fraction"]
    if type(shift) not in (int, float) or not math.isfinite(shift) or not 0.01 <= shift <= 0.5:
        raise ValueError("Edge shift must be 0.01..0.5 native pixels.")
    fractions = goal["target_fractions_native_nyquist"]
    if (
        not isinstance(fractions, list) or not 2 <= len(fractions) <= 8
        or any(type(value) not in (int, float) for value in fractions)
    ):
        raise ValueError("Declare 2..8 target fractions below native Nyquist.")
    values = [float(value) for value in fractions]
    if (
        any(not math.isfinite(value) or not 0 < value < 1 for value in values)
        or any(right <= left for left, right in zip(values, values[1:], strict=False))
    ):
        raise ValueError("Target fractions must increase below native Nyquist.")
    budget = goal["optical_tile_estimated_working_budget_bytes"]
    if type(budget) is not int or not 256 * 1024**2 <= budget <= 8 * 1024**3:
        raise ValueError("Optical tile policy must be 256 MiB..8 GiB.")
    return {
        "schema": SCHEMA,
        "name": name.strip(),
        "registered_roi": roi,
        "candidate_run_id": run_id,
        "candidate_asset_sha256": sha,
        "fixed_grid_goal": {
            "edge_shift_native_pixel_fraction": float(shift),
            "target_fractions_native_nyquist": values,
            "optical_tile_estimated_working_budget_bytes": budget,
        },
    }


def _candidate_binding(service: Any, spec: dict[str, Any]) -> dict[str, Any]:
    from .lens_dense_geometric_psf_candidate_job import get as candidate_get

    record = candidate_get(service, spec["candidate_run_id"])
    if record["status"] != "succeeded":
        raise ValueError("Dense geometric PSF candidate Run must have succeeded.")
    summary = record["result"]["summary"]
    manifest = summary["candidate_manifest"]
    if (
        manifest["candidate_asset_sha256"] != spec["candidate_asset_sha256"]
        or manifest["lens_revision_id"] != spec["registered_roi"]["lens_revision_id"]
        or manifest["optical_psf_semantics"] != "geometric_pre_pixel_ray_histogram"
        or manifest["pixel_aperture_included"] is not False
        or manifest["diffraction_included"] is not False
    ):
        raise ValueError("Candidate Run does not match the requested registered Lens.")
    return {
        "candidate_run_id": record["id"],
        "candidate_asset_sha256": manifest["candidate_asset_sha256"],
        "candidate_manifest_artifact_sha256": (
            summary["candidate_manifest_artifact"]["sha256"]
        ),
        "source_psf_asset_sha256": manifest["source_psf_asset_sha256"],
        "lens_db_sha256": manifest["lens_db_sha256"],
        "lens_revision_id": manifest["lens_revision_id"],
        "lens_revision_content_sha256": manifest["lens_revision_content_sha256"],
        "source_prescription_sha256": manifest["source_prescription_sha256"],
        "reviewed_prescription_file_sha256": (
            summary["pinned_source_binding"]["reviewed_prescription_file_sha256"]
        ),
        "candidate_psf_array_sha256": manifest["candidate_psf_array_sha256"],
    }


def _prepare_pending(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .lens_sensor_registered_roi_disk_raw_job import _prepare_pending as roi_prepare

    spec = specification(payload)
    baseline = roi_prepare(service, spec["registered_roi"])
    candidate = _candidate_binding(service, spec)
    pinned = baseline["pinned_source_binding"]
    if (
        candidate["source_psf_asset_sha256"] != pinned["geometric_psf_sha256"]
        or candidate["lens_revision_content_sha256"] != pinned["lens_revision_sha256"]
        or candidate["lens_db_sha256"] != pinned["lens_source_db_sha256"]
    ):
        raise ValueError("Candidate source differs from registered Lens revision/PSF.")
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "registered_roi_submission": baseline,
        "candidate_binding": candidate,
        "environment": baseline["environment"],
        "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
        "submission_preflight_status": "candidate_and_registered_model_worker_preflight_pending",
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = _prepare_pending(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .lens_dense_geometric_psf_candidate_job import load_verified_candidate
    from .lens_sensor_dense_psf_sensitivity import compare_fixed_grid_dense_geometric_psf
    from .lens_sensor_out_of_core_raw_job import _ANGLES_DEG, _resource_policy
    from .lens_sensor_registered_roi_disk_raw_job import (
        _execution_inputs,
        _model_identities_match_pins,
        _recheck_registered_blob,
    )
    from .lens_sensor_registered_roi_disk_raw_job import (
        _prepare_pending as roi_prepare,
    )
    from .lens_sensor_registered_roi_sampling_adapter import (
        frequency_sampling_kwargs_for_registered_roi,
    )
    from .lens_sensor_scene_geometry_tile import _optical_state_fingerprint
    from .lens_sensor_streamed_full_raw import _sensor_identity

    if (
        job.get("job_kind") != JOB_KIND
        or service.registry.get_run(job["id"])["status"] != "running"
    ):
        raise IntegrityError("Dense-PSF sensitivity Worker received a non-running job.")
    spec = specification(job["specification"])
    frozen = _prepare_pending(service, spec)
    if any(canonical(job.get(key)) != canonical(value) for key, value in frozen.items()):
        raise IntegrityError("Dense-PSF sensitivity frozen request changed.")
    roi = spec["registered_roi"]
    baseline = job["registered_roi_submission"]
    mode, binding, lens, inspected, provider, sensor, _tiles, _resident = (
        _execution_inputs(service, roi)
    )
    if (
        not _model_identities_match_pins(
            lens, inspected, baseline["pinned_source_binding"]
        )
        or lens["identity"].get("prescription_sha256")
        != job["candidate_binding"]["source_prescription_sha256"]
        or lens["identity"].get("source_db_sha256")
        != job["candidate_binding"]["lens_db_sha256"]
        or any(
            binding.get(key) != value
            for key, value in baseline["registered_mode_binding"].items()
        )
    ):
        raise IntegrityError("Loaded Lens/Sensor/active mode differs from pinned inputs.")
    candidate = job["candidate_binding"]
    manifest, arrays = load_verified_candidate(
        service, candidate["candidate_run_id"],
        candidate_asset_sha256=candidate["candidate_asset_sha256"],
        expected_lens_revision_id=roi["lens_revision_id"],
        expected_source_psf_asset_sha256=candidate["source_psf_asset_sha256"],
    )
    if (
        manifest["candidate_psf_array_sha256"]
        != candidate["candidate_psf_array_sha256"]
        or manifest["source_prescription_sha256"]
        != candidate["source_prescription_sha256"]
    ):
        raise IntegrityError("Candidate array or prescription differs from submit pin.")
    optics_before = _optical_state_fingerprint(provider.optics)
    sensor_before = _sensor_identity(sensor)
    plan_inputs = frequency_sampling_kwargs_for_registered_roi(
        roi, optics=provider.optics, sensor=sensor,
        active_mode={**mode, "id": binding["declaration_id"]},
        scene_grid_mode="explicit",
    )
    goal = spec["fixed_grid_goal"]
    native_pitch_um = float(plan_inputs["native_pixel_pitch_um_row_col"][1])
    native_nyquist = 500.0 / native_pitch_um
    image_distance_m = plan_inputs["image_distance_m"]
    edge_shift_geometry = _fixed_grid_shift_geometry(
        edge_offset_deg=roi["analytic_scene"]["edge_offset_deg"],
        native_pitch_um=native_pitch_um,
        requested_fraction=goal["edge_shift_native_pixel_fraction"],
        image_distance_m=image_distance_m,
    )
    shift_deg = edge_shift_geometry["planned_angular_shift_deg"]
    runner_kwargs = {
        "first_scene_shape_row_col": tuple(roi["analytic_scene"]["shape_row_col"]),
        "max_refinements": 0,
        "scene_horizontal_fov_deg": roi["analytic_scene"]["horizontal_fov_deg"],
        "wavelengths_nm": provider.wavelengths_nm,
        "bright_side_photons": provider.bright_photons,
        "analytic_slanted_edge_offset_deg": roi["analytic_scene"]["edge_offset_deg"],
        "fixed_shift_deg": shift_deg,
        "sensor_tile_bounds_row_col_exclusive": tuple(
            roi["sensor_roi_bounds_row_col_exclusive"]
        ),
        "source_tile_shape_row_col": tuple(roi["source_tile_shape_row_col"]),
        "sample_angles_deg": _ANGLES_DEG,
        "resource_policy": replace(
            _resource_policy().optical_tile,
            estimated_working_budget_bytes=(
                goal["optical_tile_estimated_working_budget_bytes"]
            ),
        ),
        "target_frequency_cycles_per_mm": tuple(
            fraction * native_nyquist
            for fraction in goal["target_fractions_native_nyquist"]
        ),
        "source_rasterization": "cell_area_integrated",
    }
    comparison = compare_fixed_grid_dense_geometric_psf(
        provider.optics, sensor, manifest, arrays, runner_kwargs=runner_kwargs
    )
    _recheck_registered_blob(service, roi, mode, binding)
    if (
        canonical(roi_prepare(service, roi)) != canonical(baseline)
        or canonical(_candidate_binding(service, spec)) != canonical(candidate)
        or _optical_state_fingerprint(provider.optics) != optics_before
        or _sensor_identity(sensor) != sensor_before
        or service.loaded_environment != job["environment"]
        or service.registry.get_run(job["id"])["status"] != "running"
    ):
        raise IntegrityError("Pinned inputs changed during dense-PSF sensitivity capture.")
    summary = {
        "schema": SCHEMA,
        "status": comparison["status"],
        "specification": spec,
        "frozen_submission_sha256": job["content_hash"],
        "candidate_binding": candidate,
        "registered_active_mode": binding,
        "source_identities": {
            "lens": lens["identity"],
            "sensor": inspected["revision"],
            "optical_state_sha256": optics_before,
            "sensor_model_state_sha256": sensor_before,
        },
        "comparison": comparison,
        "fixed_grid_edge_shift_geometry": edge_shift_geometry,
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
        "physical_accuracy_qualified": False,
        "limitation": LIMITATION,
    }
    descriptor = service.registry.store_blob(
        canonical(summary).encode("utf-8"), ARTIFACT_NAME, "application/json"
    )
    return {"summary": summary, "artifacts": [descriptor]}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a dense-PSF fixed-grid sensitivity job.")
    summary = (job.get("result") or {}).get("summary")
    if (job["status"] == "succeeded") != (summary is not None):
        raise IntegrityError("Dense-PSF sensitivity result differs from run status.")
    if summary is not None:
        artifacts = (job.get("result") or {}).get("artifacts") or []
        if (
            summary.get("schema") != SCHEMA
            or summary.get("frozen_submission_sha256") != job["content_hash"]
            or canonical(summary.get("specification"))
            != canonical(job["specification"])
            or summary.get("candidate_binding") != job.get("candidate_binding")
            or summary.get("status") != (summary.get("comparison") or {}).get("status")
            or summary.get("physical_mtf_comparison_allowed") is not False
            or summary.get("product_ranking_allowed") is not False
            or summary.get("physical_accuracy_qualified") is not False
            or len(artifacts) != 1 or artifacts[0].get("name") != ARTIFACT_NAME
        ):
            raise IntegrityError("Dense-PSF sensitivity result violates pinned contract.")
        binding = service.registry.artifact_binding(
            artifacts[0]["sha256"], owner_kind="runs", owner_id=identity
        )
        path = binding["path"]
        if (
            binding["descriptor"] != artifacts[0]
            or path.stat().st_size > 4 * 1024**2
            or json.loads(path.read_bytes()) != summary
        ):
            raise IntegrityError("Dense-PSF sensitivity artifact differs from result.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: run.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for run in service.registry.list_runs() if run.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/lens-sensor-dense-psf-sensitivity/template", tags=["lens_sampling"])
    def template():
        return {"specification": default_spec(), "limitation": LIMITATION}

    @app.post("/api/v1/lens-sensor-dense-psf-sensitivity", status_code=202, tags=["lens_sampling"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-dense-psf-sensitivity", tags=["lens_sampling"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-dense-psf-sensitivity/{identity}", tags=["lens_sampling"])
    def detail(identity: str):
        return get(service, identity)

    @app.post(
        "/api/v1/lens-sensor-dense-psf-sensitivity/{identity}/cancel",
        tags=["lens_sampling"],
    )
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-sensor-dense-psf-sensitivity/{identity}/export", tags=["lens_sampling"])
    def export(identity: str):
        return Response(
            canonical(get(service, identity)), media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="dense-psf-sensitivity.json"'
            },
        )
