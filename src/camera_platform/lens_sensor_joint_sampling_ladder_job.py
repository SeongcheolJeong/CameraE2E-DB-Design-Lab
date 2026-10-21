"""Durable paired Scene-grid × dense geometric PSF sensitivity study.

The registered Lens/Sensor/active-mode authority and candidate Run are pinned
at submit and checked again by the Worker.  Both optics execute each exact
fixed-FOV Scene grid.  A denser pupil candidate is not PSF-source convergence
or a product MTF qualification.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from typing import Any

from .lens_sensor_dense_psf_sensitivity_job import (
    _candidate_binding,
    _fixed_grid_shift_geometry,
)
from .lens_sensor_registered_roi_frequency_execution import (
    _REGISTERED_MAX_SCENE_AXIS,
    _REGISTERED_MAX_VIRTUAL_CELLS,
    EXTENDED_SPARSE_ROI_COORDINATE_POLICY,
    EXTENDED_SPARSE_ROI_MAX_SCENE_AXIS,
    EXTENDED_SPARSE_ROI_MAX_VIRTUAL_CELLS,
)
from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_joint_scene_dense_psf_ladder_job_v1"
JOB_KIND = "lens_sensor_joint_scene_dense_psf_ladder"
ARTIFACT_NAME = "joint-scene-dense-psf-ladder.json"
WORKER_TIMEOUT_S = 1800
EXTENDED_WORKER_TIMEOUT_S = 3600
DEFAULT_COORDINATE_POLICY = "registered_roi_v1"
LIMITATION = (
    "The registered geometric Lens/Sensor model and one opt-in higher-pupil-density "
    "PSF candidate are compared on the same fixed-FOV Scene grid ladder. The candidate "
    "does not refine the saved PSF histogram pitch, and one source-versus-candidate "
    "difference cannot establish pupil-density convergence. Bayer mosaic mean electrons "
    "do not qualify Nyquist MTF. Diffraction, field-dependent pixel angle response, "
    "physical accuracy, product ranking and full-frame execution remain unverified."
)


def default_spec() -> dict[str, Any]:
    from .lens_sensor_registered_roi_disk_raw_job import default_spec as roi_default

    roi = roi_default()
    first = roi["analytic_scene"]["shape_row_col"]
    return {
        "schema": SCHEMA,
        "name": "렌즈×센서 Scene·고밀도 PSF 결합 표본화 진단",
        "registered_roi": roi,
        "candidate_run_id": "",
        "candidate_asset_sha256": "",
        "joint_goal": {
            "scene_grid_ladder_row_col": [
                [first[0] * 2**index, first[1] * 2**index] for index in range(3)
            ],
            "coordinate_policy": DEFAULT_COORDINATE_POLICY,
            "scene_rasterization": "cell_area_slanted_edge",
            "edge_shift_native_pixel_fraction": 0.25,
            "target_fractions_native_nyquist": [0.25, 0.9],
            "mean_electron_full_scale_normalized_max_delta_tolerance": 0.01,
            "shift_response_full_scale_normalized_max_delta_tolerance": 0.02,
            "optical_tile_estimated_working_budget_bytes": 512 * 1024**2,
        },
    }


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    selected = float(value)
    if not minimum <= selected <= maximum:
        raise ValueError(f"{name} must be within [{minimum}, {maximum}].")
    return selected


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    from .lens_sensor_registered_roi_disk_raw_job import specification as roi_spec

    template = default_spec()
    if not isinstance(payload, dict) or set(payload) != set(template):
        raise ValueError("Declare the exact joint Scene/PSF ladder fields.")
    if payload["schema"] != SCHEMA:
        raise ValueError("Unsupported joint Scene/PSF ladder schema.")
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Name requires 1..160 characters.")
    roi = roi_spec(payload["registered_roi"])
    if roi["capture"]["noise_flag"] != 0:
        raise ValueError("Joint mean-electron ladder requires noise_flag=0.")
    run_id = payload["candidate_run_id"]
    asset_sha = payload["candidate_asset_sha256"]
    if not isinstance(run_id, str) or re.fullmatch(r"run-[0-9a-f]{32}", run_id) is None:
        raise ValueError("Choose one completed candidate Run ID.")
    if not isinstance(asset_sha, str) or re.fullmatch(r"[0-9a-f]{64}", asset_sha) is None:
        raise ValueError("Choose the candidate NPZ artifact SHA-256.")
    goal = payload["joint_goal"]
    if not isinstance(goal, dict) or set(goal) != set(template["joint_goal"]):
        raise ValueError("Declare the exact joint_goal fields.")
    if goal["scene_rasterization"] != "cell_area_slanted_edge":
        raise ValueError("Joint ladder requires cell-area slanted-edge rasterization.")
    coordinate = goal["coordinate_policy"]
    if coordinate == DEFAULT_COORDINATE_POLICY:
        max_axis = _REGISTERED_MAX_SCENE_AXIS
        max_cells = _REGISTERED_MAX_VIRTUAL_CELLS
    elif coordinate == EXTENDED_SPARSE_ROI_COORDINATE_POLICY:
        max_axis = EXTENDED_SPARSE_ROI_MAX_SCENE_AXIS
        max_cells = EXTENDED_SPARSE_ROI_MAX_VIRTUAL_CELLS
    else:
        raise ValueError("Unsupported joint Scene coordinate policy.")
    ladder = goal["scene_grid_ladder_row_col"]
    if not isinstance(ladder, list) or not 3 <= len(ladder) <= 5:
        raise ValueError("Declare 3..5 exact Scene grids.")
    if ladder[0] != roi["analytic_scene"]["shape_row_col"]:
        raise ValueError("First Scene grid must equal the registered ROI Scene shape.")
    if coordinate == EXTENDED_SPARSE_ROI_COORDINATE_POLICY and len(ladder) != 4:
        raise ValueError("Extended sparse-ROI policy currently requires four grids.")
    grids = []
    for shape in ladder:
        if (
            not isinstance(shape, list) or len(shape) != 2
            or any(type(axis) is not int or axis < 32 or axis % 2 for axis in shape)
        ):
            raise ValueError("Each Scene grid needs two even integer axes >=32.")
        row, col = shape
        if max(shape) > max_axis or row * col > max_cells:
            raise ValueError("Scene grid exceeds its declared coordinate policy.")
        if grids and shape != [2 * axis for axis in grids[-1]]:
            raise ValueError("Scene grids must double both axes at fixed FOV.")
        grids.append([row, col])
    shift = _number(
        goal["edge_shift_native_pixel_fraction"], "Edge shift", 0.01, 0.5
    )
    frequencies = goal["target_fractions_native_nyquist"]
    if not isinstance(frequencies, list) or not 2 <= len(frequencies) <= 8:
        raise ValueError("Declare 2..8 target fractions below native Nyquist.")
    targets = [
        _number(value, "Target Nyquist fraction", 1e-6, 0.999999)
        for value in frequencies
    ]
    if any(high <= low for low, high in zip(targets, targets[1:], strict=False)):
        raise ValueError("Target fractions must increase below native Nyquist.")
    mean_tol = _number(
        goal["mean_electron_full_scale_normalized_max_delta_tolerance"],
        "Mean-electron tolerance", 1e-6, 0.1,
    )
    shift_tol = _number(
        goal["shift_response_full_scale_normalized_max_delta_tolerance"],
        "Shift-response tolerance", 1e-6, 0.1,
    )
    budget = goal["optical_tile_estimated_working_budget_bytes"]
    if type(budget) is not int or not 256 * 1024**2 <= budget <= 8 * 1024**3:
        raise ValueError("Optical tile estimate policy must be 256 MiB..8 GiB.")
    return {
        "schema": SCHEMA,
        "name": name.strip(),
        "registered_roi": roi,
        "candidate_run_id": run_id,
        "candidate_asset_sha256": asset_sha,
        "joint_goal": {
            "scene_grid_ladder_row_col": grids,
            "coordinate_policy": coordinate,
            "scene_rasterization": "cell_area_slanted_edge",
            "edge_shift_native_pixel_fraction": shift,
            "target_fractions_native_nyquist": targets,
            "mean_electron_full_scale_normalized_max_delta_tolerance": mean_tol,
            "shift_response_full_scale_normalized_max_delta_tolerance": shift_tol,
            "optical_tile_estimated_working_budget_bytes": budget,
        },
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
        "default_worker_timeout_seconds": (
            EXTENDED_WORKER_TIMEOUT_S
            if spec["joint_goal"]["coordinate_policy"]
            == EXTENDED_SPARSE_ROI_COORDINATE_POLICY else WORKER_TIMEOUT_S
        ),
        "submission_preflight_status": "candidate_and_registered_model_worker_preflight_pending",
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = _prepare_pending(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .lens_dense_geometric_psf_candidate_job import load_verified_candidate
    from .lens_sensor_dense_psf_sensitivity import (
        optics_with_dense_geometric_psf_candidate,
    )
    from .lens_sensor_joint_sampling_ladder import run_joint_scene_dense_psf_ladder
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
        raise IntegrityError("Joint Scene/PSF Worker received a non-running job.")
    spec = specification(job["specification"])
    frozen = _prepare_pending(service, spec)
    if any(canonical(job.get(key)) != canonical(value) for key, value in frozen.items()):
        raise IntegrityError("Joint Scene/PSF frozen request changed.")
    roi = spec["registered_roi"]
    baseline = job["registered_roi_submission"]
    mode, binding, lens, inspected, provider, sensor, _tiles, _resident = (
        _execution_inputs(service, roi)
    )
    candidate = job["candidate_binding"]
    if (
        not _model_identities_match_pins(
            lens, inspected, baseline["pinned_source_binding"]
        )
        or lens["identity"].get("prescription_sha256")
        != candidate["source_prescription_sha256"]
        or lens["identity"].get("source_db_sha256") != candidate["lens_db_sha256"]
        or any(
            binding.get(key) != value
            for key, value in baseline["registered_mode_binding"].items()
        )
    ):
        raise IntegrityError("Loaded Lens/Sensor/active mode differs from pinned inputs.")
    manifest, arrays = load_verified_candidate(
        service, candidate["candidate_run_id"],
        candidate_asset_sha256=candidate["candidate_asset_sha256"],
        expected_lens_revision_id=roi["lens_revision_id"],
        expected_source_psf_asset_sha256=candidate["source_psf_asset_sha256"],
    )
    if (
        manifest["candidate_psf_array_sha256"] != candidate["candidate_psf_array_sha256"]
        or manifest["source_prescription_sha256"]
        != candidate["source_prescription_sha256"]
        or manifest["lens_db_sha256"] != candidate["lens_db_sha256"]
        or manifest["lens_revision_content_sha256"]
        != candidate["lens_revision_content_sha256"]
    ):
        raise IntegrityError("Candidate array, prescription or source differs from submit pin.")
    candidate_optics = optics_with_dense_geometric_psf_candidate(
        provider.optics, manifest, arrays
    )
    source_before = _optical_state_fingerprint(provider.optics)
    candidate_before = _optical_state_fingerprint(candidate_optics)
    sensor_before = _sensor_identity(sensor)
    plan_inputs = frequency_sampling_kwargs_for_registered_roi(
        roi, optics=provider.optics, sensor=sensor,
        active_mode={**mode, "id": binding["declaration_id"]},
        scene_grid_mode="explicit",
    )
    goal = spec["joint_goal"]
    native_pitch_um = float(plan_inputs["native_pixel_pitch_um_row_col"][1])
    nyquist = 500.0 / native_pitch_um
    geometry = _fixed_grid_shift_geometry(
        edge_offset_deg=roi["analytic_scene"]["edge_offset_deg"],
        native_pitch_um=native_pitch_um,
        requested_fraction=goal["edge_shift_native_pixel_fraction"],
        image_distance_m=plan_inputs["image_distance_m"],
    )
    policy = goal["coordinate_policy"]
    if policy == EXTENDED_SPARSE_ROI_COORDINATE_POLICY:
        max_axis = EXTENDED_SPARSE_ROI_MAX_SCENE_AXIS
        max_cells = EXTENDED_SPARSE_ROI_MAX_VIRTUAL_CELLS
    else:
        max_axis = _REGISTERED_MAX_SCENE_AXIS
        max_cells = _REGISTERED_MAX_VIRTUAL_CELLS
    runner_kwargs = {
        "scene_horizontal_fov_deg": roi["analytic_scene"]["horizontal_fov_deg"],
        "wavelengths_nm": provider.wavelengths_nm,
        "bright_side_photons": provider.bright_photons,
        "analytic_slanted_edge_offset_deg": roi["analytic_scene"]["edge_offset_deg"],
        "fixed_shift_deg": geometry["planned_angular_shift_deg"],
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
            fraction * nyquist for fraction in goal["target_fractions_native_nyquist"]
        ),
        "source_rasterization": "cell_area_integrated",
        "max_scene_axis": max_axis,
        "max_virtual_scene_cells": max_cells,
        "coordinate_policy": policy,
    }
    joint = run_joint_scene_dense_psf_ladder(
        provider.optics, candidate_optics, sensor,
        scene_grid_ladder_row_col=goal["scene_grid_ladder_row_col"],
        runner_kwargs=runner_kwargs,
        mean_delta_tolerance=(
            goal["mean_electron_full_scale_normalized_max_delta_tolerance"]
        ),
        shift_delta_tolerance=(
            goal["shift_response_full_scale_normalized_max_delta_tolerance"]
        ),
    )
    _recheck_registered_blob(service, roi, mode, binding)
    if (
        canonical(roi_prepare(service, roi)) != canonical(baseline)
        or canonical(_candidate_binding(service, spec)) != canonical(candidate)
        # Candidate get() rechecks typed owner/manifest; blob_path() also
        # rehashes the archive bytes after the long numerical capture.
        or not service.registry.blob_path(candidate["candidate_asset_sha256"])
        or _optical_state_fingerprint(provider.optics) != source_before
        or _optical_state_fingerprint(candidate_optics) != candidate_before
        or _sensor_identity(sensor) != sensor_before
        or service.loaded_environment != job["environment"]
        or service.registry.get_run(job["id"])["status"] != "running"
    ):
        raise IntegrityError("Pinned models or candidate changed during joint capture.")
    summary = {
        "schema": SCHEMA,
        "status": joint["status"],
        "specification": spec,
        "frozen_submission_sha256": job["content_hash"],
        "candidate_binding": candidate,
        "registered_active_mode": binding,
        "source_identities": {
            "lens": lens["identity"],
            "sensor": inspected["revision"],
            "optical_state_sha256": source_before,
            "candidate_optical_state_sha256": candidate_before,
            "sensor_model_state_sha256": sensor_before,
        },
        "joint_ladder": joint,
        "edge_shift_geometry": geometry,
        "coordinate_policy": policy,
        "policy_is_hardware_limit": False,
        "independent_pupil_density_convergence_established": False,
        "psf_histogram_pitch_refinement_assessed": False,
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
    from .lens_sensor_registered_roi_disk_raw_job import _model_identities_match_pins

    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected Run is not a joint Scene/PSF ladder job.")
    summary = (job.get("result") or {}).get("summary")
    if (job["status"] == "succeeded") != (summary is not None):
        raise IntegrityError("Joint Scene/PSF result differs from Run status.")
    if summary is not None:
        artifacts = (job.get("result") or {}).get("artifacts") or []
        baseline = job.get("registered_roi_submission") or {}
        identities = summary.get("source_identities") or {}
        joint = summary.get("joint_ladder") or {}
        binding = baseline.get("registered_mode_binding") or {}
        actual_mode = summary.get("registered_active_mode") or {}
        if (
            summary.get("schema") != SCHEMA
            or summary.get("frozen_submission_sha256") != job["content_hash"]
            or canonical(summary.get("specification"))
            != canonical(job["specification"])
            or summary.get("candidate_binding") != job.get("candidate_binding")
            or not baseline.get("pinned_source_binding")
            or not _model_identities_match_pins(
                {"identity": identities.get("lens") or {}},
                {"revision": identities.get("sensor") or {}},
                baseline["pinned_source_binding"],
            )
            or any(actual_mode.get(key) != value for key, value in binding.items())
            or summary.get("status") != joint.get("status")
            or summary.get("coordinate_policy")
            != job["specification"]["joint_goal"]["coordinate_policy"]
            or any(summary.get(key) is not False for key in (
                "policy_is_hardware_limit",
                "independent_pupil_density_convergence_established",
                "psf_histogram_pitch_refinement_assessed",
                "physical_mtf_comparison_allowed",
                "product_ranking_allowed",
                "physical_accuracy_qualified",
            ))
            or any(joint.get(key) is not False for key in (
                "independent_pupil_density_convergence_established",
                "psf_histogram_pitch_refinement_assessed",
                "physical_mtf_comparison_allowed",
                "product_ranking_allowed",
                "physical_accuracy_qualified",
            ))
            or len(artifacts) != 1 or artifacts[0].get("name") != ARTIFACT_NAME
        ):
            raise IntegrityError("Joint Scene/PSF result violates its pinned contract.")
        owner = service.registry.artifact_binding(
            artifacts[0]["sha256"], owner_kind="runs", owner_id=identity
        )
        path = owner["path"]
        if (
            owner["descriptor"] != artifacts[0]
            or path.stat().st_size > 4 * 1024**2
            or json.loads(path.read_bytes()) != summary
        ):
            raise IntegrityError("Joint Scene/PSF artifact differs from saved result.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: run.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for run in service.registry.list_runs() if run.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    endpoint = "/api/v1/lens-sensor-joint-sampling-ladder"

    @app.get(f"{endpoint}/template", tags=["lens_sampling"])
    def template():
        return {"specification": default_spec(), "limitation": LIMITATION}

    @app.post(endpoint, status_code=202, tags=["lens_sampling"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get(endpoint, tags=["lens_sampling"])
    def listing():
        return list_records(service)

    @app.get(f"{endpoint}/{{identity}}", tags=["lens_sampling"])
    def detail(identity: str):
        return get(service, identity)

    @app.post(f"{endpoint}/{{identity}}/cancel", tags=["lens_sampling"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get(f"{endpoint}/{{identity}}/export", tags=["lens_sampling"])
    def export(identity: str):
        return Response(
            canonical(get(service, identity)), media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="joint-scene-dense-psf-ladder.json"'
            },
        )
