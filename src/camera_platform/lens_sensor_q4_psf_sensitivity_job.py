"""Durable one-grid registered Lens x Sensor sensitivity to an opt-in q4 PSF.

The registered Lens/Sensor/active mode and the q4 rebin Run are pinned at
submission and rechecked by the Worker. The source and q4 optical models see
the same fixed-FOV analytic Scene and sensor ROI. This is a controlled
geometric-PSF sensitivity calculation, not PSF or camera MTF qualification.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from typing import Any

from .lens_sensor_dense_psf_sensitivity_job import _fixed_grid_shift_geometry
from .lens_sensor_registered_roi_frequency_execution import (
    _REGISTERED_MAX_SCENE_AXIS,
    _REGISTERED_MAX_VIRTUAL_CELLS,
    EXTENDED_SPARSE_ROI_COORDINATE_POLICY,
    EXTENDED_SPARSE_ROI_MAX_SCENE_AXIS,
    EXTENDED_SPARSE_ROI_MAX_VIRTUAL_CELLS,
)
from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_q4_psf_fixed_grid_sensitivity_job_v1"
COMPARISON_SCHEMA = "camera_lens_sensor_q4_psf_fixed_grid_sensitivity_v1"
JOB_KIND = "lens_sensor_q4_psf_sensitivity"
ARTIFACT_NAME = "q4-psf-fixed-grid-sensitivity.json"
WORKER_TIMEOUT_S = 1800
EXTENDED_WORKER_TIMEOUT_S = 3600
LIMITATION = (
    "One fixed-FOV analytic Scene/OI grid and one registered sensor ROI compare the "
    "stored geometric source PSF to an opt-in dense-ray quarter-pitch PSF. Both "
    "pupil sampling and histogram pitch differ between those two assets, so their "
    "camera delta cannot isolate histogram pitch. Source radiometry/direction and "
    "the sensor operator remain fixed. This single grid does not establish grid "
    "convergence, diffraction, physical MTF, full-frame behavior, physical accuracy "
    "or product ranking. Virtual-grid and memory bounds are policies, not hardware "
    "limits or peak-RSS guarantees."
)


def default_spec() -> dict[str, Any]:
    from .lens_sensor_registered_roi_disk_raw_job import default_spec as roi_default

    roi = roi_default()
    return {
        "schema": SCHEMA,
        "name": "렌즈×센서 q4 PSF 고정 격자 민감도",
        "registered_roi": roi,
        "candidate_run_id": "",
        "candidate_asset_sha256": "",
        "fixed_grid_goal": {
            "scene_shape_row_col": roi["analytic_scene"]["shape_row_col"].copy(),
            "coordinate_policy": "registered_roi_v1",
            "edge_shift_native_pixel_fraction": 0.25,
            "target_fractions_native_nyquist": [0.25, 0.9],
            "optical_tile_estimated_working_budget_bytes": 512 * 1024**2,
        },
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    from .lens_sensor_registered_roi_disk_raw_job import specification as roi_spec

    template = default_spec()
    if not isinstance(payload, dict) or set(payload) != set(template):
        raise ValueError("Declare the exact q4 fixed-grid sensitivity job fields.")
    if payload["schema"] != SCHEMA:
        raise ValueError("Unsupported q4 fixed-grid sensitivity schema.")
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Name requires 1..160 characters.")
    roi = roi_spec(payload["registered_roi"])
    if roi["capture"]["noise_flag"] != 0:
        raise ValueError("q4 PSF fixed-grid mean comparison requires noise_flag=0.")
    run_id = payload["candidate_run_id"]
    sha = payload["candidate_asset_sha256"]
    if not isinstance(run_id, str) or re.fullmatch(r"run-[0-9a-f]{32}", run_id) is None:
        raise ValueError("Choose one completed q4 candidate Run ID.")
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
        raise ValueError("Choose the q4 candidate NPZ artifact SHA-256.")
    goal = payload["fixed_grid_goal"]
    if not isinstance(goal, dict) or set(goal) != set(template["fixed_grid_goal"]):
        raise ValueError("Declare the exact fixed_grid_goal fields.")
    policy = goal["coordinate_policy"]
    if policy == "registered_roi_v1":
        max_axis = _REGISTERED_MAX_SCENE_AXIS
        max_cells = _REGISTERED_MAX_VIRTUAL_CELLS
    elif policy == EXTENDED_SPARSE_ROI_COORDINATE_POLICY:
        max_axis = EXTENDED_SPARSE_ROI_MAX_SCENE_AXIS
        max_cells = EXTENDED_SPARSE_ROI_MAX_VIRTUAL_CELLS
    else:
        raise ValueError("Unsupported q4 fixed-grid coordinate policy.")
    shape = goal["scene_shape_row_col"]
    if (
        not isinstance(shape, list) or len(shape) != 2
        or any(type(axis) is not int or axis < 32 or axis % 2 for axis in shape)
        or max(shape) > max_axis or math.prod(shape) > max_cells
    ):
        raise ValueError("Scene grid exceeds the declared coordinate policy.")
    shift = goal["edge_shift_native_pixel_fraction"]
    if (
        type(shift) not in (int, float) or not math.isfinite(shift)
        or not 0.01 <= shift <= 0.5
    ):
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
            "scene_shape_row_col": list(shape),
            "coordinate_policy": policy,
            "edge_shift_native_pixel_fraction": float(shift),
            "target_fractions_native_nyquist": values,
            "optical_tile_estimated_working_budget_bytes": budget,
        },
    }


def _candidate_binding(service: Any, spec: dict[str, Any]) -> dict[str, Any]:
    from .lens_dense_geometric_psf_q4_rebin_job import get as candidate_get

    record = candidate_get(service, spec["candidate_run_id"])
    if record["status"] != "succeeded":
        raise ValueError("q4 geometric PSF rebin Run must have succeeded.")
    summary = record["result"]["summary"]
    manifest = summary["candidate_manifest"]
    parent = summary["pinned_parent_and_lens_binding"]
    archive = summary["candidate_archive_artifact"]
    if (
        manifest["candidate_asset_sha256"] != spec["candidate_asset_sha256"]
        or archive["sha256"] != spec["candidate_asset_sha256"]
        or manifest["lens_revision_id"] != spec["registered_roi"]["lens_revision_id"]
        or manifest["optical_psf_semantics"] != "geometric_pre_pixel_ray_histogram"
        or manifest["pixel_aperture_included"] is not False
        or manifest["diffraction_included"] is not False
        or manifest["histogram_refinement_factor"] != 4
        or manifest["histogram_gap_within_declared_tolerance_model_only"] is not True
        or manifest["histogram_target_status"]
        != "numeric_gap_within_declared_tolerance_model_only"
    ):
        raise ValueError("q4 Run does not match the requested registered Lens.")
    return {
        "candidate_run_id": record["id"],
        "candidate_run_content_hash": record["content_hash"],
        "candidate_asset_sha256": archive["sha256"],
        "candidate_manifest_artifact_sha256": (
            summary["candidate_manifest_artifact"]["sha256"]
        ),
        "candidate_psf_array_sha256": manifest["candidate_psf_array_sha256"],
        "parent_candidate_run_id": parent["parent_candidate_run_id"],
        "parent_candidate_asset_sha256": parent["parent_candidate_asset_sha256"],
        "source_psf_asset_sha256": manifest["source_psf_asset_sha256"],
        "lens_db_sha256": manifest["lens_db_sha256"],
        "lens_revision_id": manifest["lens_revision_id"],
        "lens_revision_content_sha256": manifest["lens_revision_content_sha256"],
        "source_prescription_sha256": manifest["source_prescription_sha256"],
        "q4_histogram_pitch_mm_row_col": manifest["candidate_histogram_pitch_mm_row_col"],
        "q4_psf_shape_row_col_field_wave": (
            manifest["candidate_psf_shape_row_col_field_wave"]
        ),
        "histogram_gap_within_declared_tolerance_model_only": (
            manifest["histogram_gap_within_declared_tolerance_model_only"]
        ),
        "estimated_working_array_bytes_not_peak_rss": (
            manifest["estimated_working_array_bytes_not_peak_rss"]
        ),
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
        or candidate["estimated_working_array_bytes_not_peak_rss"]
        > spec["fixed_grid_goal"]["optical_tile_estimated_working_budget_bytes"]
    ):
        raise ValueError("q4 source or declared memory policy differs from registered Lens.")
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
            if spec["fixed_grid_goal"]["coordinate_policy"]
            == EXTENDED_SPARSE_ROI_COORDINATE_POLICY else WORKER_TIMEOUT_S
        ),
        "submission_preflight_status": (
            "owner_bound_q4_candidate_and_registered_mode_worker_preflight_pending"
        ),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = _prepare_pending(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def compare_fixed_grid_q4_geometric_psf(
    source_optics: dict[str, Any], sensor: Any,
    candidate_report: dict[str, Any], candidate_arrays: dict[str, Any],
    *, runner_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Execute exactly one source/q4 pair with identical Scene and sensor inputs."""
    from .lens_sensor_joint_sampling_ladder import (
        _capture_arrays,
        _full_scale_delta,
        _phase_step,
        _physical_input,
    )
    from .lens_sensor_q4_psf_overlay import optics_with_q4_geometric_psf_candidate
    from .lens_sensor_registered_roi_frequency_execution import (
        run_registered_roi_frequency_sampling,
    )
    from .lens_sensor_scene_geometry_tile import _optical_state_fingerprint
    from .lens_sensor_streamed_full_raw import _sensor_identity

    if runner_kwargs.get("max_refinements") != 0:
        raise ValueError("q4 sensitivity requires exactly one explicit Scene grid.")
    shape = tuple(runner_kwargs["first_scene_shape_row_col"])
    candidate_optics = optics_with_q4_geometric_psf_candidate(
        source_optics, candidate_report, candidate_arrays
    )
    source_before = _optical_state_fingerprint(source_optics)
    candidate_before = _optical_state_fingerprint(candidate_optics)
    sensor_before = _sensor_identity(sensor)
    if source_before == candidate_before:
        raise IntegrityError("q4 overlay did not change optical state.")
    source_capture, source_values = run_registered_roi_frequency_sampling(
        source_optics, sensor, return_transient_means=True, **runner_kwargs
    )
    candidate_capture, candidate_values = run_registered_roi_frequency_sampling(
        candidate_optics, sensor, return_transient_means=True, **runner_kwargs
    )
    if (
        _optical_state_fingerprint(source_optics) != source_before
        or _optical_state_fingerprint(candidate_optics) != candidate_before
        or _sensor_identity(sensor) != sensor_before
    ):
        raise IntegrityError("Source, q4 or Sensor state changed during fixed-grid comparison.")
    source_means = _capture_arrays(source_values)
    candidate_means = _capture_arrays(candidate_values)
    report: dict[str, Any] = {
        "schema": COMPARISON_SCHEMA,
        "status": "inconclusive_fixed_grid_capture_unavailable",
        "source_psf_asset_sha256": candidate_report["source_psf_asset_sha256"],
        "candidate_psf_asset_sha256": candidate_report["candidate_asset_sha256"],
        "source_optical_state_sha256": source_before,
        "candidate_optical_state_sha256": candidate_before,
        "sensor_identity": sensor_before,
        "scene_shape_row_col": list(shape),
        "coordinate_policy": runner_kwargs["coordinate_policy"],
        "source_capture_status": source_capture["status"],
        "candidate_capture_status": candidate_capture["status"],
        "source_step": source_capture["steps"][-1] if source_capture["steps"] else None,
        "candidate_step": (
            candidate_capture["steps"][-1] if candidate_capture["steps"] else None
        ),
        "same_scene_and_sensor_inputs": False,
        "source_vs_q4_changes_pupil_density_and_histogram_pitch": True,
        "histogram_pitch_only_effect_isolated": False,
        "scene_sampling_convergence_established": False,
        "pupil_density_convergence_qualified_all_nodes": False,
        "diffraction_included": False,
        "readout_or_isp_executed": False,
        "physical_mtf_comparison_allowed": False,
        "physical_accuracy_qualified": False,
        "product_ranking_allowed": False,
    }
    if source_means is None or candidate_means is None:
        return report
    source_step = _phase_step(source_capture, shape)
    candidate_step = _phase_step(candidate_capture, shape)
    if (
        source_capture.get("sensor_identity") != sensor_before
        or candidate_capture.get("sensor_identity") != sensor_before
        or _physical_input(source_capture, across_grids=False)
        != _physical_input(candidate_capture, across_grids=False)
        or source_step["source_identity_by_phase"]
        != candidate_step["source_identity_by_phase"]
        or source_step["oi_pitch_um"] != candidate_step["oi_pitch_um"]
        or any(a.shape != b.shape for a, b in zip(
            source_means, candidate_means, strict=True
        ))
    ):
        raise IntegrityError("Source and q4 did not use identical Scene/Sensor inputs.")
    delta = _full_scale_delta(source_means, candidate_means)
    valid = (
        source_step.get("mean_phase_signal_valid") is True
        and candidate_step.get("mean_phase_signal_valid") is True
    )
    report.update({
        "status": (
            "numeric_q4_geometric_psf_fixed_grid_sensitivity_model_only"
            if valid else "inconclusive_edge_signal_support"
        ),
        "same_scene_and_sensor_inputs": True,
        "edge_signal_valid_for_both_optics": valid,
        "scene_oi_pitch_um": source_step["oi_pitch_um"],
        "source_mean_electrons_sha256_by_phase": (
            source_step["mean_electrons_sha256_by_phase"]
        ),
        "candidate_mean_electrons_sha256_by_phase": (
            candidate_step["mean_electrons_sha256_by_phase"]
        ),
        "local_signal_relative_error_assessed": False,
        **delta,
    })
    return report


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .lens_dense_geometric_psf_q4_rebin_job import load_verified_rebin
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
        raise IntegrityError("q4 sensitivity Worker received a non-running job.")
    spec = specification(job["specification"])
    frozen = _prepare_pending(service, spec)
    if any(canonical(job.get(key)) != canonical(value) for key, value in frozen.items()):
        raise IntegrityError("q4 sensitivity frozen request changed.")
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
        raise IntegrityError("Loaded Lens/Sensor/active mode differs from submit pin.")
    manifest, arrays = load_verified_rebin(
        service, candidate["candidate_run_id"],
        candidate_asset_sha256=candidate["candidate_asset_sha256"],
        expected_parent_run_id=candidate["parent_candidate_run_id"],
        expected_parent_asset_sha256=candidate["parent_candidate_asset_sha256"],
        expected_lens_revision_id=roi["lens_revision_id"],
        expected_source_psf_asset_sha256=candidate["source_psf_asset_sha256"],
    )
    if (
        manifest["candidate_psf_array_sha256"]
        != candidate["candidate_psf_array_sha256"]
        or manifest["source_prescription_sha256"]
        != candidate["source_prescription_sha256"]
        or manifest["candidate_histogram_pitch_mm_row_col"]
        != candidate["q4_histogram_pitch_mm_row_col"]
        or manifest["candidate_psf_shape_row_col_field_wave"]
        != candidate["q4_psf_shape_row_col_field_wave"]
    ):
        raise IntegrityError("Loaded q4 array, prescription or grid differs from submit pin.")
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
    geometry = _fixed_grid_shift_geometry(
        edge_offset_deg=roi["analytic_scene"]["edge_offset_deg"],
        native_pitch_um=native_pitch_um,
        requested_fraction=goal["edge_shift_native_pixel_fraction"],
        image_distance_m=plan_inputs["image_distance_m"],
    )
    if goal["coordinate_policy"] == EXTENDED_SPARSE_ROI_COORDINATE_POLICY:
        max_axis = EXTENDED_SPARSE_ROI_MAX_SCENE_AXIS
        max_cells = EXTENDED_SPARSE_ROI_MAX_VIRTUAL_CELLS
    else:
        max_axis = _REGISTERED_MAX_SCENE_AXIS
        max_cells = _REGISTERED_MAX_VIRTUAL_CELLS
    runner_kwargs = {
        "first_scene_shape_row_col": tuple(goal["scene_shape_row_col"]),
        "max_refinements": 0,
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
            fraction * native_nyquist
            for fraction in goal["target_fractions_native_nyquist"]
        ),
        "source_rasterization": "cell_area_integrated",
        "max_scene_axis": max_axis,
        "max_virtual_scene_cells": max_cells,
        "coordinate_policy": goal["coordinate_policy"],
    }
    comparison = compare_fixed_grid_q4_geometric_psf(
        provider.optics, sensor, manifest, arrays, runner_kwargs=runner_kwargs
    )
    _recheck_registered_blob(service, roi, mode, binding)
    if (
        canonical(roi_prepare(service, roi)) != canonical(baseline)
        or canonical(_candidate_binding(service, spec)) != canonical(candidate)
        or not service.registry.blob_path(candidate["candidate_asset_sha256"])
        or _optical_state_fingerprint(provider.optics) != optics_before
        or _sensor_identity(sensor) != sensor_before
        or service.loaded_environment != job["environment"]
        or service.registry.get_run(job["id"])["status"] != "running"
    ):
        raise IntegrityError("Pinned models or q4 artifact changed during capture.")
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
        "fixed_grid_edge_shift_geometry": geometry,
        "coordinate_policy": goal["coordinate_policy"],
        "policy_is_hardware_limit": False,
        "scene_sampling_convergence_established": False,
        "histogram_pitch_only_effect_isolated": False,
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
        raise ValueError("Selected Run is not a q4 PSF fixed-grid sensitivity job.")
    summary = (job.get("result") or {}).get("summary")
    if (job["status"] == "succeeded") != (summary is not None):
        raise IntegrityError("q4 sensitivity result differs from Run status.")
    if summary is not None:
        artifacts = (job.get("result") or {}).get("artifacts") or []
        baseline = job.get("registered_roi_submission") or {}
        identities = summary.get("source_identities") or {}
        binding = baseline.get("registered_mode_binding") or {}
        actual_mode = summary.get("registered_active_mode") or {}
        comparison = summary.get("comparison") or {}
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
            or summary.get("status") != comparison.get("status")
            or comparison.get("schema") != COMPARISON_SCHEMA
            or summary.get("coordinate_policy")
            != job["specification"]["fixed_grid_goal"]["coordinate_policy"]
            or comparison.get("scene_shape_row_col")
            != job["specification"]["fixed_grid_goal"]["scene_shape_row_col"]
            or any(summary.get(key) is not False for key in (
                "policy_is_hardware_limit",
                "scene_sampling_convergence_established",
                "histogram_pitch_only_effect_isolated",
                "physical_mtf_comparison_allowed",
                "product_ranking_allowed",
                "physical_accuracy_qualified",
            ))
            or any(comparison.get(key) is not False for key in (
                "histogram_pitch_only_effect_isolated",
                "scene_sampling_convergence_established",
                "pupil_density_convergence_qualified_all_nodes",
                "diffraction_included",
                "readout_or_isp_executed",
                "physical_mtf_comparison_allowed",
                "physical_accuracy_qualified",
                "product_ranking_allowed",
            ))
            or (
                comparison.get("status")
                == "numeric_q4_geometric_psf_fixed_grid_sensitivity_model_only"
                and comparison.get("same_scene_and_sensor_inputs") is not True
            )
            or len(artifacts) != 1 or artifacts[0].get("name") != ARTIFACT_NAME
        ):
            raise IntegrityError("q4 sensitivity result violates pinned contract.")
        owner = service.registry.artifact_binding(
            artifacts[0]["sha256"], owner_kind="runs", owner_id=identity
        )
        path = owner["path"]
        if (
            owner["descriptor"] != artifacts[0]
            or path.stat().st_size > 4 * 1024**2
            or json.loads(path.read_bytes()) != summary
        ):
            raise IntegrityError("q4 sensitivity artifact differs from Run summary.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: run.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for run in service.registry.list_runs() if run.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    route = "/api/v1/lens-sensor-q4-psf-sensitivity"

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
                "Content-Disposition": 'attachment; filename="q4-psf-sensitivity.json"'
            },
        )
