"""Durable, bounded frequency-targeted Lens x Sensor sampling study.

The registered ROI specification pins the existing Lens/Sensor/active-mode
authority. Sampling controls belong to this new job contract; legacy RAW jobs
and their stored results keep their original interpretation.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_frequency_target_study_v1"
EXTENDED_SCHEMA = "camerae2e_lens_sensor_frequency_target_study_v2"
JOB_KIND = "lens_sensor_frequency_target"
WORKER_TIMEOUT_S = 1800
EXTENDED_WORKER_TIMEOUT_S = 3600
LIMITATION = (
    "This study evaluates the selected numerical geometric-PSF and saved sensor "
    "model. A Scene/OI grid change also changes the executed PSF interpolation grid. "
    "Bayer mosaic mean-electron capture does not by itself establish system SFR "
    "through the native-pixel Nyquist. Stored-PSF interpolation is not source "
    "resolution refinement; diffraction, field-dependent pixel angle response, "
    "measured product FOV/MTF and full-frame execution remain unverified."
)


def default_spec() -> dict[str, Any]:
    from .lens_sensor_registered_roi_disk_raw_job import default_spec as roi_default_spec

    return {
        "schema": SCHEMA,
        "name": "렌즈×센서 목표 주파수 표본화 진단",
        "registered_roi": roi_default_spec(),
        "sampling_goal": {
            "mode": "adaptive",
            "observable": "pre_readout_mean_electrons",
            "frequency_direction": "image_plane_column_x_right",
            "target_fraction_native_nyquist": 0.9,
            "minimum_scene_samples_per_target_period_seed": 4,
            "evaluation_fractions_native_nyquist": [0.25, 0.5, 0.75, 0.9],
            "max_refinements": 2,
            "mean_electron_full_scale_normalized_max_delta_tolerance": 0.01,
            "shift_response_full_scale_normalized_max_delta_tolerance": 0.02,
            "mtf_absolute_tolerance": 0.02,
            "fixed_shift_pixel_fraction": 0.25,
            "minimum_normalized_signal_contrast": 0.05,
            "optical_tile_estimated_working_budget_bytes": 512 * 1024**2,
            "scene_rasterization": "cell_area_slanted_edge",
            "psf_resolution_mode": "stored_geometric_only",
            "psf_target_pitch_um": None,
            "alias_probe_grid_seed_multiple_native_nyquist": None,
        },
    }


def extended_default_spec() -> dict[str, Any]:
    """Opt in to a larger *virtual* grid for this frequency ROI study only."""
    from .lens_sensor_registered_roi_frequency_execution import (
        EXTENDED_SPARSE_ROI_COORDINATE_POLICY,
    )

    result = default_spec()
    result["schema"] = EXTENDED_SCHEMA
    result["sampling_goal"]["coordinate_policy"] = (
        EXTENDED_SPARSE_ROI_COORDINATE_POLICY
    )
    return result


def _number(value: Any, label: str, *, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a finite numeric value.")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{label} must be within [{minimum}, {maximum}].")
    return number


def _fixed_tangent_plane_shift_deg(
    *, edge_offset_deg: float, requested_shift_um: float, image_distance_m: float,
) -> float:
    """Convert an image-plane displacement into the edge's angular shift.

    The analytic Scene is positioned on the image tangent plane.  Away from
    the optical axis, equal angular steps do not give equal image-plane steps.
    This sets the requested displacement *before* lens distortion; it cannot
    qualify the local shift after the registered lens geometry is applied.
    """
    if (
        not all(math.isfinite(value) for value in (
            edge_offset_deg, requested_shift_um, image_distance_m
        ))
        or not -90 < edge_offset_deg < 90
        or requested_shift_um <= 0
        or image_distance_m <= 0
    ):
        raise ValueError("Edge angle, requested shift and image distance are invalid.")
    base = math.radians(edge_offset_deg)
    moved = math.atan(
        math.tan(base) + requested_shift_um * 1e-6 / image_distance_m
    )
    delta_deg = math.degrees(moved - base)
    if delta_deg <= 0 or not math.isfinite(delta_deg):
        raise ValueError("Requested tangent-plane shift cannot be represented in angle.")
    return delta_deg


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate an exact versioned request; numerical goals are not promises."""
    from .lens_sensor_registered_roi_disk_raw_job import specification as roi_specification

    if not isinstance(payload, dict):
        raise ValueError("Declare the exact frequency-target study fields.")
    schema = payload.get("schema")
    if schema == SCHEMA:
        template = default_spec()
    elif schema == EXTENDED_SCHEMA:
        template = extended_default_spec()
    else:
        raise ValueError("Unsupported frequency-target study schema.")
    if not isinstance(payload, dict) or set(payload) != set(template):
        raise ValueError("Declare the exact frequency-target study fields.")
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Name requires 1..160 characters.")
    roi = roi_specification(payload["registered_roi"])
    if roi["capture"]["noise_flag"] != 0:
        raise ValueError("Frequency-target mean-electron study requires noise_flag=0.")
    goal = payload["sampling_goal"]
    expected = template["sampling_goal"]
    if not isinstance(goal, dict) or set(goal) != set(expected):
        raise ValueError("Declare the exact sampling_goal fields.")
    if schema == EXTENDED_SCHEMA and (
        goal["coordinate_policy"] != expected["coordinate_policy"]
    ):
        raise ValueError("Unsupported extended frequency ROI coordinate policy.")
    mode = goal["mode"]
    if mode not in {"adaptive", "explicit"}:
        raise ValueError("Sampling mode must be adaptive or explicit.")
    if goal["observable"] != "pre_readout_mean_electrons":
        raise ValueError("This study supports only pre-readout mean electrons.")
    if goal["frequency_direction"] != "image_plane_column_x_right":
        raise ValueError("This study supports only the image-plane column direction.")
    refinements = goal["max_refinements"]
    allowed_refinements = {0, 2, 3, 4} if mode == "explicit" else {2, 3, 4}
    if type(refinements) is not int or refinements not in allowed_refinements:
        raise ValueError("Explicit mode permits 0 or 2..4 refinements; adaptive requires 2..4.")
    target = _number(
        goal["target_fraction_native_nyquist"], "Target Nyquist fraction",
        minimum=0.05, maximum=1.0,
    )
    samples = goal["minimum_scene_samples_per_target_period_seed"]
    if type(samples) is not int or not 2 <= samples <= 16:
        raise ValueError("Scene period search seed needs 2..16 samples.")
    fractions = goal["evaluation_fractions_native_nyquist"]
    if (
        not isinstance(fractions, list) or not 2 <= len(fractions) <= 16
        or any(type(item) not in (int, float) for item in fractions)
    ):
        raise ValueError("Declare 2..16 numeric frequency evaluation fractions.")
    evaluated = [float(item) for item in fractions]
    if (
        not all(math.isfinite(item) and 0 < item < 1 for item in evaluated)
        or any(right <= left for left, right in zip(evaluated, evaluated[1:], strict=False))
        or evaluated[-1] > target
    ):
        raise ValueError("Evaluation frequencies must increase below Nyquist and target.")
    tolerances = {
        "mean_electron_full_scale_normalized_max_delta_tolerance": (1e-6, 0.1),
        "shift_response_full_scale_normalized_max_delta_tolerance": (1e-6, 0.1),
        "mtf_absolute_tolerance": (1e-6, 0.1),
        "fixed_shift_pixel_fraction": (0.01, 0.5),
        "minimum_normalized_signal_contrast": (1e-4, 0.5),
    }
    normalized = deepcopy(goal)
    normalized["target_fraction_native_nyquist"] = target
    normalized["evaluation_fractions_native_nyquist"] = evaluated
    for field, (low, high) in tolerances.items():
        normalized[field] = _number(goal[field], field, minimum=low, maximum=high)
    budget = goal["optical_tile_estimated_working_budget_bytes"]
    if type(budget) is not int or not 256 * 1024**2 <= budget <= 8 * 1024**3:
        raise ValueError("Optical tile working estimate policy must be 256 MiB..8 GiB.")
    if goal["scene_rasterization"] != "cell_area_slanted_edge":
        raise ValueError("Only cell-area slanted-edge rasterization is supported in this study.")
    psf_mode = goal["psf_resolution_mode"]
    if psf_mode not in {"stored_geometric_only", "pinned_same_ray_retrace_diagnostic"}:
        raise ValueError("Unsupported PSF resolution mode.")
    psf_pitch = goal["psf_target_pitch_um"]
    if psf_mode == "stored_geometric_only":
        if psf_pitch is not None:
            raise ValueError("Stored PSF mode cannot declare an invented generation pitch.")
    else:
        if psf_pitch is None:
            if mode != "adaptive":
                raise ValueError("Explicit retrace needs a declared PSF target pitch.")
        else:
            normalized["psf_target_pitch_um"] = _number(
                psf_pitch, "PSF target pitch", minimum=0.01, maximum=100.0
            )
    alias = goal["alias_probe_grid_seed_multiple_native_nyquist"]
    if alias is not None:
        normalized["alias_probe_grid_seed_multiple_native_nyquist"] = _number(
            alias, "Alias probe multiple", minimum=1.01, maximum=8.0
        )
    return {
        "schema": schema,
        "name": name.strip(),
        "registered_roi": roi,
        "sampling_goal": normalized,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Queue with the same cheap pinned-source boundary as registered ROI RAW."""
    from .lens_sensor_registered_roi_disk_raw_job import _prepare_pending

    spec = specification(payload)
    base = _prepare_pending(service, spec["registered_roi"])
    frozen = {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": spec["schema"],
        "specification": spec,
        "registered_roi_submission": base,
        "environment": base["environment"],
        "default_worker_timeout_seconds": (
            EXTENDED_WORKER_TIMEOUT_S
            if spec["schema"] == EXTENDED_SCHEMA else WORKER_TIMEOUT_S
        ),
        "submission_preflight_status": "worker_preflight_pending",
        "limitation": LIMITATION,
    }
    return service.registry.submit(frozen, digest(frozen))


def get(service: Any, identity: str) -> dict[str, Any]:
    from .lens_sensor_registered_roi_disk_raw_job import _model_identities_match_pins

    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a frequency-target sampling study.")
    summary = (job.get("result") or {}).get("summary")
    if (job["status"] == "succeeded") != (summary is not None):
        raise IntegrityError("Frequency-target terminal result differs from run status.")
    if summary is not None:
        frozen_base = job.get("registered_roi_submission") or {}
        source = summary.get("source_identities") or {}
        pinned = frozen_base.get("pinned_source_binding") or {}
        registered = frozen_base.get("registered_mode_binding") or {}
        detailed_mode = summary.get("registered_active_mode") or {}
        if (
            summary.get("schema") != job.get("specification", {}).get("schema")
            or summary.get("frozen_submission_sha256") != job["content_hash"]
            or canonical(summary.get("specification")) != canonical(job["specification"])
            or not pinned
            or not _model_identities_match_pins(
                {"identity": source.get("lens") or {}},
                {"revision": source.get("sensor") or {}}, pinned,
            )
            or any(detailed_mode.get(key) != value for key, value in registered.items())
            or summary.get("physical_accuracy_qualified") is not False
            or summary.get("physical_mtf_comparison_allowed") is not False
            or summary.get("product_ranking_allowed") is not False
        ):
            raise IntegrityError("Frequency-target result differs from the frozen request.")
        artifacts = (job.get("result") or {}).get("artifacts") or []
        if len(artifacts) != 1 or artifacts[0].get("name") != "frequency-target-study.json":
            raise IntegrityError("Frequency-target result artifact is missing.")
        import json

        path = service.registry.blob_path(artifacts[0]["sha256"])
        if path.stat().st_size > 4 * 1024**2:
            raise IntegrityError("Frequency-target result artifact exceeds size policy.")
        encoded = path.read_bytes()
        if json.loads(encoded) != summary:
            raise IntegrityError("Frequency-target artifact differs from the saved summary.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs()
        if item.get("job_kind") == JOB_KIND
    ]


def _stored_psf_note(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "inconclusive_independent_psf_source_refinement_not_run",
        "method": "stored_geometric_psf_resampled_for_scene_grid",
        "stored_pitch_um_row_col": plan["stored_psf_pitch_um_row_col"],
        "stored_grid_covers_target_band_necessary_only": (
            plan["stored_psf_grid_covers_target_band_necessary_only"]
        ),
        "source_resolution_gained_by_interpolation": False,
        "independent_psf_source_convergence_established": False,
        "diffraction_included": False,
        "physical_mtf_comparison_allowed": False,
    }


def _plain_json(value: Any) -> Any:
    """Convert scalar outputs from numerical helpers without admitting arrays."""
    import numpy as np

    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        raise IntegrityError("Frequency-target summary cannot contain a numerical array.")
    if isinstance(value, dict):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    """Run a frozen registered model through fixed-FOV sampling refinement.

    The terminal status describes supported numerical evidence. A completed
    job can be inconclusive; the Worker must not reinterpret that as a product
    accuracy or release qualification.
    """
    from dataclasses import replace

    from .lens_sensor_frequency_sampling import plan_frequency_sampling
    from .lens_sensor_out_of_core_raw_job import _ANGLES_DEG, _db_root, _resource_policy
    from .lens_sensor_registered_roi_disk_raw_job import (
        _execution_inputs,
        _model_identities_match_pins,
        _prepare_pending,
        _recheck_registered_blob,
    )
    from .lens_sensor_registered_roi_frequency_execution import (
        run_registered_roi_frequency_sampling,
    )
    from .lens_sensor_registered_roi_sampling_adapter import (
        frequency_sampling_kwargs_for_registered_roi,
    )
    from .lens_sensor_scene_geometry_tile import _optical_state_fingerprint
    from .lens_sensor_streamed_full_raw import _sensor_identity
    from .offline_assets import offline_asset_identity

    if (
        job.get("job_kind") != JOB_KIND
        or service.registry.get_run(job["id"])["status"] != "running"
    ):
        raise IntegrityError("Frequency-target Worker received a non-running or wrong-kind job.")
    spec = specification(job["specification"])
    frozen_base = job.get("registered_roi_submission")
    if (
        canonical(spec) != canonical(job["specification"])
        or not isinstance(frozen_base, dict)
        or canonical(_prepare_pending(service, spec["registered_roi"])) != canonical(frozen_base)
        or service.loaded_environment != job.get("environment")
    ):
        raise IntegrityError("Pinned frequency-target request or executable environment changed.")
    roi = spec["registered_roi"]
    goal = spec["sampling_goal"]
    inputs = _execution_inputs(service, roi)
    mode, binding, lens, inspected, provider, sensor, _tiles, _resident = inputs
    if (
        any(
            binding.get(key) != value
            for key, value in frozen_base["registered_mode_binding"].items()
        )
        or not _model_identities_match_pins(
            lens, inspected, frozen_base["pinned_source_binding"]
        )
    ):
        raise IntegrityError("Loaded Lens, Sensor or active mode differs from pinned inputs.")
    optics_before = _optical_state_fingerprint(provider.optics)
    sensor_before = _sensor_identity(sensor)
    kwargs = frequency_sampling_kwargs_for_registered_roi(
        roi, optics=provider.optics, sensor=sensor,
        active_mode={**mode, "id": binding["declaration_id"]},
        scene_grid_mode=goal["mode"],
    )
    kwargs.update(
        target_fraction_native_nyquist=goal["target_fraction_native_nyquist"],
        minimum_scene_samples_per_target_period=(
            goal["minimum_scene_samples_per_target_period_seed"]
        ),
        alias_probe_multiple_native_nyquist=(
            goal["alias_probe_grid_seed_multiple_native_nyquist"]
        ),
        max_refinements=max(2, goal["max_refinements"]),
    )
    if spec["schema"] == EXTENDED_SCHEMA:
        from .lens_sensor_registered_roi_frequency_execution import (
            EXTENDED_SPARSE_ROI_MAX_SCENE_AXIS,
            EXTENDED_SPARSE_ROI_MAX_VIRTUAL_CELLS,
        )

        kwargs["max_scene_axis"] = EXTENDED_SPARSE_ROI_MAX_SCENE_AXIS
        kwargs["max_virtual_scene_cells"] = (
            EXTENDED_SPARSE_ROI_MAX_VIRTUAL_CELLS
        )
    plan = plan_frequency_sampling(**kwargs)
    selected = plan["selected_starting_shape_row_col"]
    # A zero-refinement explicit request is a useful numerical capture, but
    # can never report convergence. Adaptive needs the two comparisons up
    # front; under the policy cap we return a bounded inconclusive report.
    required_grids = 1 if goal["max_refinements"] == 0 else 3
    first_grid_available = (
        len(plan["fixed_fov_refinement_grids"]) >= required_grids
        and plan["selected_starting_grid_meets_registered_native_pixel_floor"] is True
    )
    reference_means = None
    runner_kwargs = None
    if not first_grid_available:
        execution = {
            "status": "inconclusive_refinement_policy",
            "reason": "Declared coordinate policy cannot admit the required fixed-FOV grid ladder.",
            "mean_phase_status": "not_assessed",
            "frequency_target_points_status": "not_assessed",
            "frequency_coverage_scope": "declared_discrete_frequency_points_only",
            "steps": [],
            "readout_or_isp_executed": False,
            "physical_mtf_comparison_allowed": False,
            "product_ranking_allowed": False,
        }
    else:
        pixel_pitch_um = float(mode["pixel_pitch_um"])
        requested_shift_um = pixel_pitch_um * goal["fixed_shift_pixel_fraction"]
        shift_deg = _fixed_tangent_plane_shift_deg(
            edge_offset_deg=roi["analytic_scene"]["edge_offset_deg"],
            requested_shift_um=requested_shift_um,
            image_distance_m=kwargs["image_distance_m"],
        )
        nyquist_col = 500.0 / kwargs["native_pixel_pitch_um_row_col"][1]
        targets = tuple(
            fraction * nyquist_col
            for fraction in goal["evaluation_fractions_native_nyquist"]
        )
        runner_kwargs = {
            "first_scene_shape_row_col": tuple(selected),
            "max_refinements": goal["max_refinements"],
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
            "target_frequency_cycles_per_mm": targets,
            "source_rasterization": "cell_area_integrated",
            "mean_electron_full_scale_normalized_max_delta_tolerance": (
                goal["mean_electron_full_scale_normalized_max_delta_tolerance"]
            ),
            "shift_response_full_scale_normalized_max_delta_tolerance": (
                goal["shift_response_full_scale_normalized_max_delta_tolerance"]
            ),
            "maximum_absolute_mtf_delta": goal["mtf_absolute_tolerance"],
            "minimum_normalized_edge_contrast": goal["minimum_normalized_signal_contrast"],
            "max_scene_axis": kwargs["max_scene_axis"],
            "max_virtual_scene_cells": kwargs["max_virtual_scene_cells"],
        }
        if spec["schema"] == EXTENDED_SCHEMA:
            runner_kwargs["coordinate_policy"] = goal["coordinate_policy"]
        if goal["psf_resolution_mode"] == "pinned_same_ray_retrace_diagnostic":
            execution, reference_means = run_registered_roi_frequency_sampling(
                provider.optics, sensor, return_transient_means=True, **runner_kwargs
            )
        else:
            execution = run_registered_roi_frequency_sampling(
                provider.optics, sensor, **runner_kwargs
            )
        actual_shift_um = (execution.get("fixed_physical_input") or {}).get(
            "edge_phase_shift_tangent_plane_um"
        )
        if actual_shift_um is not None and not math.isclose(
            actual_shift_um, requested_shift_um, rel_tol=1e-9, abs_tol=1e-9
        ):
            raise IntegrityError("Executed tangent-plane edge shift differs from request.")
        execution["edge_shift_geometry"] = {
            "requested_native_pixel_fraction": goal["fixed_shift_pixel_fraction"],
            "requested_tangent_plane_um": requested_shift_um,
            "planned_angular_shift_deg": shift_deg,
            "executed_tangent_plane_um": actual_shift_um,
            "distorted_local_edge_shift_qualified": False,
        }

    last_executed_step = next(
        (
            step for step in reversed(execution["steps"])
            if step.get("mean_electrons_sha256_by_phase")
        ), None,
    )
    execution["scene_pitch_search_seed_um"] = plan["scene_pitch_search_seed_um"]
    execution["executed_finest_pitch_meets_frequency_search_seed"] = (
        last_executed_step is not None
        and last_executed_step["oi_pitch_um"] <= plan["scene_pitch_search_seed_um"]
    )
    execution["alias_probe_executed"] = False
    psf_diagnostic = _stored_psf_note(plan)
    if goal["psf_resolution_mode"] == "pinned_same_ray_retrace_diagnostic":
        from .lens_sensor_psf_refinement import prepare_pinned_geometric_psf_target_band

        fields = provider.optics["raytrace"]["psf"]["field_height_mm"]
        wavelengths = provider.optics["raytrace"]["psf"]["wavelength_nm"]
        # This exact saved node diagnoses histogram-bin sensitivity. The ROI
        # can be between nodes, so this is not a local off-axis MTF assertion.
        chosen_field = float(fields[len(fields) // 2])
        chosen_wave = float(wavelengths[len(wavelengths) // 2])
        last_shape = execution.get("last_executed_scene_shape_row_col")
        last_step = next(
            (
                step for step in reversed(execution["steps"])
                if step.get("mean_electrons_sha256_by_phase")
            ), None,
        )
        capture_pitch = last_step["oi_pitch_um"] if last_step else None
        requested_pitch = goal["psf_target_pitch_um"]
        if requested_pitch is None:
            pitch = (
                capture_pitch if capture_pitch is not None
                else min(
                    plan["psf_generation_pitch_search_seed_um"],
                    min(plan["stored_psf_pitch_um_row_col"]) / 2,
                )
            )
        else:
            pitch = requested_pitch
        same_grid_pitch = (
            capture_pitch is not None
            and math.isclose(pitch, capture_pitch, rel_tol=1e-9, abs_tol=1e-9)
        )
        try:
            prepared = prepare_pinned_geometric_psf_target_band(
                roi["lens_revision_id"], catalog_root=_db_root(service),
                target_pitch_um=pitch, field_height_mm=chosen_field,
                wavelength_nm=chosen_wave,
                target_frequencies_cycles_per_mm=tuple(
                    fraction * 500.0 / kwargs["native_pixel_pitch_um_row_col"][1]
                    for fraction in goal["evaluation_fractions_native_nyquist"]
                ),
                directions_deg=(0.0, 90.0),
            )
        except ValueError as exc:
            # Some older saved geometric PSFs cannot be reproduced from
            # their pinned prescription. This is an explicit unsupported
            # model, never a successful refined-PSF capture.
            if str(exc) not in {
                "Rebuilt chief ray differs from pinned PSF asset.",
                "Retraced successful-ray mask differs from pinned PSF asset.",
                "Retraced source-grid PSF does not reproduce pinned histogram.",
            }:
                raise
            psf_diagnostic = {
                "status": "inconclusive_pinned_psf_source_reproduction_failed",
                "reason": str(exc),
                "target_pitch_um": pitch,
                "psf_source_resolution_qualified": False,
                "same_grid_camera_comparison_executed": False,
                "physical_mtf_comparison_allowed": False,
            }
            prepared = None
        if prepared is not None:
            psf_diagnostic = prepared.evidence
        psf_diagnostic["selected_node_is_exact_roi_local_field"] = False
        source_asset = psf_diagnostic.get("source_psf_asset_sha256")
        if (
            source_asset is not None
            and source_asset != frozen_base["pinned_source_binding"]["geometric_psf_sha256"]
        ):
            raise IntegrityError("Retraced PSF source differs from pinned Lens asset.")
        if (
            prepared is not None and prepared.optics is not None
            and reference_means is not None and last_shape and same_grid_pitch
        ):
            derived_kwargs = {
                **runner_kwargs,
                "first_scene_shape_row_col": tuple(last_shape),
                "max_refinements": 0,
            }
            derived_report, derived_means = run_registered_roi_frequency_sampling(
                prepared.optics, sensor, return_transient_means=True,
                **derived_kwargs,
            )
            comparison = {
                "status": "inconclusive_derived_psf_capture_blocked",
                "scene_shape_row_col": last_shape,
                "source_oi_pitch_um": capture_pitch,
                "derived_psf_pitch_um": pitch,
                "derived_capture_status": derived_report["status"],
                "same_grid_camera_comparison_executed": False,
                "independent_psf_source_convergence_established": False,
            }
            if derived_means is not None:
                import numpy as np

                scale = max(
                    1e-12,
                    *(float(np.max(np.abs(array))) for array in (
                        *reference_means, *derived_means
                    )),
                )
                mean_delta = max(
                    float(np.max(np.abs(fine - coarse))) / scale
                    for coarse, fine in zip(
                        reference_means, derived_means, strict=True
                    )
                )
                phase_delta = float(np.max(np.abs(
                    (derived_means[1] - derived_means[0])
                    - (reference_means[1] - reference_means[0])
                ))) / scale
                comparison.update({
                    "status": "numeric_same_ray_psf_grid_sensitivity_model_only",
                    "same_grid_camera_comparison_executed": True,
                    "full_scale_electrons_denominator": scale,
                    "mean_electron_full_scale_normalized_max_delta": mean_delta,
                    "shift_response_full_scale_normalized_max_delta": phase_delta,
                    "mean_electron_tolerance_met": (
                        mean_delta <= goal[
                            "mean_electron_full_scale_normalized_max_delta_tolerance"
                        ]
                    ),
                    "phase_response_tolerance_met": (
                        phase_delta <= goal[
                            "shift_response_full_scale_normalized_max_delta_tolerance"
                        ]
                    ),
                    "derived_mean_electrons_sha256_by_phase": (
                        derived_report["steps"][-1]["mean_electrons_sha256_by_phase"]
                    ),
                })
            psf_diagnostic["fixed_grid_camera_sensitivity"] = comparison
        else:
            psf_diagnostic["fixed_grid_camera_sensitivity"] = {
                "status": (
                    "inconclusive_psf_pitch_differs_from_executed_oi_grid"
                    if not same_grid_pitch and capture_pitch is not None
                    else "inconclusive_no_reference_or_refined_psf_capture"
                ),
                "source_oi_pitch_um": capture_pitch,
                "derived_psf_pitch_um": pitch,
                "same_grid_camera_comparison_executed": False,
                "independent_psf_source_convergence_established": False,
            }
    _recheck_registered_blob(service, roi, mode, binding)
    if (
        _optical_state_fingerprint(provider.optics) != optics_before
        or _sensor_identity(sensor) != sensor_before
        or offline_asset_identity(workspace=service.workspace, source=_db_root(service))
        != frozen_base["upstream_assets"]
        or canonical(_prepare_pending(service, roi)) != canonical(frozen_base)
        or service.loaded_environment != job["environment"]
        or service.registry.get_run(job["id"])["status"] != "running"
    ):
        raise IntegrityError("Pinned numerical inputs changed during sampling study.")
    aggregate_status = execution["status"]
    if (
        goal["psf_resolution_mode"] == "pinned_same_ray_retrace_diagnostic"
        and aggregate_status.startswith("numeric_")
    ):
        aggregate_status = "inconclusive_independent_psf_source_resolution"
    summary = _plain_json({
        "schema": spec["schema"],
        "status": aggregate_status,
        "specification": spec,
        "frozen_submission_sha256": job["content_hash"],
        "registered_active_mode": binding,
        "source_identities": {
            "lens": lens["identity"],
            "sensor": inspected["revision"],
            "optical_state_sha256": optics_before,
            "sensor_model_state_sha256": sensor_before,
        },
        "sampling_plan": plan,
        "execution": execution,
        "psf_target_band_diagnostic": psf_diagnostic,
        "capture_seed_used": False,
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
        "physical_accuracy_qualified": False,
        "limitation": LIMITATION,
    })
    descriptor = service.registry.store_blob(
        canonical(summary).encode(), "frequency-target-study.json", "application/json"
    )
    return {"summary": summary, "artifacts": [descriptor]}


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/lens-sensor-frequency-target/template", tags=["lens_sensor_sampling"])
    def template():
        return {"specification": default_spec(), "limitation": LIMITATION}

    @app.get(
        "/api/v1/lens-sensor-frequency-target/template/extended-sparse-roi",
        tags=["lens_sensor_sampling"],
    )
    def extended_template():
        return {"specification": extended_default_spec(), "limitation": LIMITATION}

    @app.post(
        "/api/v1/lens-sensor-frequency-target", status_code=202,
        tags=["lens_sensor_sampling"],
    )
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-frequency-target", tags=["lens_sensor_sampling"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-frequency-target/{identity}", tags=["lens_sensor_sampling"])
    def detail(identity: str):
        return get(service, identity)

    @app.post(
        "/api/v1/lens-sensor-frequency-target/{identity}/cancel",
        tags=["lens_sensor_sampling"],
    )
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get(
        "/api/v1/lens-sensor-frequency-target/{identity}/export",
        tags=["lens_sensor_sampling"],
    )
    def export(identity: str):
        return Response(
            canonical(get(service, identity)), media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="frequency-target-study.json"'},
        )
