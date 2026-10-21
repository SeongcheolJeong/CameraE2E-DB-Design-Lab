"""A common center-coordinate crop cannot stand in for physical input support."""

import math
from copy import deepcopy

import numpy as np
import pytest
from test_lens_sensor_quality_study import SLOTS, WORKSPACE, _angular_edge_fixture, _matrix

from camera_platform import isp_v2
from camera_platform.lens_sensor_common_angular_mtf import (
    _model_input_support_diagnostic,
    evaluate_common_grid_edge,
)
from camera_platform.lens_sensor_mtf_capture_preflight import (
    _conditional_single_sample_mosaic_radius_samples,
    _strict_geometry_max_inverse_factor,
    plan_same_sampling_mtf_recapture,
    require_supported_same_sampling_recapture,
)
from camera_platform.lens_sensor_quality_job import (
    _saved_shared_metric_matches,
    execute,
    get,
    submit,
)
from camera_platform.lens_sensor_quality_study import (
    _executed_psf_source_support,
    _matched_angular_edge,
)
from camera_platform.lens_sensor_raw_bridge import _scene_samples
from camera_platform.registry import IntegrityError, canonical
from camera_platform.service import PlatformService


def _evaluate(steps=None):
    cases, images, masks, comparison = _angular_edge_fixture(steps)
    native = _matched_angular_edge(cases, images, masks, comparison)
    return cases, evaluate_common_grid_edge(cases, images, masks, comparison, native)


@pytest.mark.parametrize("equal_pitches", [False, True])
def test_common_coordinates_and_equal_pixel_cells_do_not_qualify_physical_support(equal_pitches):
    _, result = _evaluate(dict.fromkeys(SLOTS, 0.001) if equal_pitches else None)
    diagnostic = result["physical_mtf_comparability"]
    assert result["available"] is True
    assert diagnostic["common_point_center_roi_matched"] is True
    assert diagnostic["model_diagnostic_available"] is True
    assert diagnostic["status"] == "not_established"
    assert diagnostic["physical_mtf_comparison_allowed"] is False
    assert diagnostic["product_ranking_allowed"] is False
    assert diagnostic["equal_native_pixel_apertures_or_psf_shapes_required"] is False
    assert diagnostic["per_system_optical_pixel_isp_input_support_verified"] is None
    assert diagnostic["broadband_object_coordinate_support_verified"] is None
    assert diagnostic["resampling_boundary_extension_independence_verified"] is None
    assert "unequal_pixel" not in " ".join(diagnostic["unmet_requirements"])
    proxy = result["model_550nm_point_cell_footprint"]
    assert proxy["proxy_within_one_percent_all_axes_and_area"] is equal_pitches


def test_actual_uniform_scored_roi_is_the_estimator_crop_not_the_full_grid():
    _, result = _evaluate()
    grid = result["grid"]
    assert grid["grid_geometry"] == "uniform_central_square"
    roi = result["physical_mtf_comparability"]["scored_point_center_roi"]
    height, width = grid["shape_row_col"]
    ry, rx = max(1, height // 8), max(1, width // 8)
    row_axis = np.linspace(*grid["row_angle_deg"], height)
    col_axis = np.linspace(*grid["column_angle_deg"], width)
    assert roi["scored_shape_row_col"] == [height - 2 * ry, width - 2 * rx]
    assert roi["scored_roi_row_col_exclusive"] == [[ry, height - ry], [rx, width - rx]]
    assert roi["row_angle_deg"] == [row_axis[ry], row_axis[-ry - 1]]
    assert roi["column_angle_deg"] == [col_axis[rx], col_axis[-rx - 1]]
    assert roi["row_angle_deg"][0] > grid["row_angle_deg"][0]
    assert roi["physical_input_support"] == "not_established"


def test_reference_grid_roi_uses_actual_saved_coordinates_without_uniformization():
    steps = dict(zip(SLOTS, (.0010408186, .0011943820, .0016707399, .0019172426), strict=True))
    cases, result = _evaluate(steps)
    grid = result["grid"]
    assert grid["grid_geometry"] == "vetted_native_reference_point_centers"
    assert grid["reference_slot"] == "L2S2"
    roi = result["physical_mtf_comparability"]["scored_point_center_roi"]
    point = cases["L2S2"]["conditions"]["native_output_angular_grid"][
        "distortion_aware_point_center"
    ]["coordinates_deg_row_col_by_wavelength"]
    (r0, r1), (c0, c1) = grid["reference_native_roi_row_col_exclusive"]
    expected = np.asarray(point)[1, r0 + 2:r1 - 2, c0 + 2:c1 - 2]
    assert roi["scored_shape_row_col"] == [12, 12]
    assert roi["row_angle_deg"] == [expected[..., 0].min(), expected[..., 0].max()]
    assert roi["column_angle_deg"] == [expected[..., 1].min(), expected[..., 1].max()]
    np.testing.assert_array_equal(
        roi["boundary_corners_deg_row_col"], expected[[0, 0, -1, -1], [0, -1, -1, 0]]
    )


def test_unavailable_grid_has_explicit_reasons_and_no_invented_scored_support():
    cases, images, masks, comparison = _angular_edge_fixture()
    result = evaluate_common_grid_edge(cases, images, masks, comparison, {"available": False})
    diagnostic = result["physical_mtf_comparability"]
    assert result["available"] is False
    assert diagnostic["model_diagnostic_available"] is False
    assert diagnostic["common_point_center_roi_matched"] is False
    assert diagnostic["scored_point_center_roi"] is None
    assert "common_point_center_roi_unavailable" in diagnostic["unmet_requirements"]
    assert "common_grid_edge_metric_unavailable" in diagnostic["unmet_requirements"]


def test_unchecked_author_claims_cannot_upgrade_missing_support():
    cases, images, masks, comparison = _angular_edge_fixture()
    for case in cases.values():
        case["conditions"]["physical_input_support_verified"] = True
        case["conditions"]["pixel_integrated_effective_area_matched"] = True
    comparison["physical_mtf_comparison_allowed"] = True
    native = _matched_angular_edge(cases, images, masks, comparison)
    result = evaluate_common_grid_edge(cases, images, masks, comparison, native)
    assert result["physical_mtf_comparability"]["physical_mtf_comparison_allowed"] is False
    assert result["product_ranking_allowed"] is False


def test_scored_stage_support_uses_actual_vertices_scene_margins_and_boundary_modes():
    cases, images, masks, comparison = _angular_edge_fixture()
    for case in cases.values():
        case["isp_implementation"] = isp_v2.IMPLEMENTATION
        case["isp_crop_full_frame_halo_native_pixels"] = 1
        case["conditions"]["native_output_angular_grid"][
            "distortion_aware_point_center"
        ]["input_scene_support"] = {
            "status": "all_model_point_centers_within_source_samples",
            "source_sample_center_support_deg_row_col": [0.02, 0.02]
        }
    native = _matched_angular_edge(cases, images, masks, comparison)
    result = evaluate_common_grid_edge(
        cases, images, masks, comparison, native,
        isp_profile=isp_v2.default_profile(),
    )
    stage = result["model_input_support_diagnostic"]
    assert stage["available"] is True
    assert stage["all_scored_resampling_boundaries_independent"] is True
    assert stage["all_fixed_isp_crop_boundaries_outside_scored_halo"] is True
    assert stage["all_scored_point_centers_within_source_sample_centers"] is True
    for slot in SLOTS:
        item = stage["per_slot"][slot]
        assert item["scored_reflect_vs_zero_boundary_max_rgb_difference"] == 0
        assert item["scored_vertex_min_native_frame_border_margin_pixels"] >= max(
            item["scored_antialias_radius_native_pixels_row_col"]
        )
        assert item["fixed_quality_isp_halo_upper_bound_native_pixels"] == 1
        assert np.asarray(
            item["scored_point_center_to_scene_sample_center_margin_deg_by_wavelength_row_col"]
        ).shape == (3, 2)
        assert min(np.asarray(
            item["scored_point_center_to_scene_sample_center_margin_deg_by_wavelength_row_col"]
        ).ravel()) > 0
    assert stage["optical_psf_input_support_verified"] is False
    assert stage["pixel_integration_input_support_verified"] is False
    assert stage["source_scene_halo_verified"] is False
    assert stage["sampling_and_edge_estimator_convergence_verified"] is False
    assert result["physical_mtf_comparability"]["physical_mtf_comparison_allowed"] is False


def test_point_centers_outside_declared_scene_support_do_not_become_halo_proof():
    cases, images, masks, comparison = _angular_edge_fixture()
    for case in cases.values():
        case["conditions"]["native_output_angular_grid"][
            "distortion_aware_point_center"
        ]["input_scene_support"] = {
            "status": "all_model_point_centers_within_source_samples",
            "source_sample_center_support_deg_row_col": [0.005, 0.005]
        }
    native = _matched_angular_edge(cases, images, masks, comparison)
    result = evaluate_common_grid_edge(cases, images, masks, comparison, native)
    stage = result["model_input_support_diagnostic"]
    assert stage["all_scored_point_centers_within_source_sample_centers"] is False
    assert all(
        any(value < 0 for row in stage["per_slot"][slot][
            "scored_point_center_to_scene_sample_center_margin_deg_by_wavelength_row_col"
        ] for value in row)
        for slot in SLOTS
    )
    assert stage["physical_mtf_comparison_allowed"] is False


def test_fixed_isp_halo_requires_matching_profile_implementation_and_summary():
    cases, images, masks, comparison = _angular_edge_fixture()
    profile = isp_v2.default_profile()
    profile["linear_sharpen"]["mode"] = "bounded_detail_v1"
    for case in cases.values():
        case["isp_implementation"] = isp_v2.IMPLEMENTATION
        case["isp_crop_full_frame_halo_native_pixels"] = 2
    native = _matched_angular_edge(cases, images, masks, comparison)
    matched = evaluate_common_grid_edge(
        cases, images, masks, comparison, native, isp_profile=profile,
    )["model_input_support_diagnostic"]
    assert matched["all_fixed_isp_crop_boundaries_outside_scored_halo"] is True
    assert all(
        matched["per_slot"][slot]["fixed_quality_isp_halo_upper_bound_native_pixels"] == 2
        for slot in SLOTS
    )

    cases["L1S1"]["isp_crop_full_frame_halo_native_pixels"] = 1
    mismatched = evaluate_common_grid_edge(
        cases, images, masks, comparison, native, isp_profile=profile,
    )["model_input_support_diagnostic"]
    assert mismatched["all_fixed_isp_crop_boundaries_outside_scored_halo"] is None
    assert mismatched["per_slot"]["L1S1"][
        "fixed_quality_isp_halo_upper_bound_native_pixels"
    ] is None
    assert mismatched["physical_mtf_comparison_allowed"] is False
    cases["L1S1"]["isp_implementation"] = None  # Historical job; no backfill.
    old = evaluate_common_grid_edge(
        cases, images, masks, comparison, native, isp_profile=profile,
    )["model_input_support_diagnostic"]
    assert old["per_slot"]["L1S1"]["fixed_isp_crop_boundary_outside_scored_halo"] is None


def test_executed_global_psf_extent_exposes_finite_source_scene_gap():
    cases, images, masks, comparison = _angular_edge_fixture()
    profile = isp_v2.default_profile()
    for case in cases.values():
        case["isp_implementation"] = isp_v2.IMPLEMENTATION
        case["isp_crop_full_frame_halo_native_pixels"] = 1
        case["conditions"]["sensor_pixel_pitch_um"] = 1.0
        case["conditions"]["model_psf_source_support"] = {
            "status": "global_exact_nonzero_bound_recorded",
            "scene_samples_row_col": [64, 64],
            "oi_sample_spacing_um": 0.25,
            "executed_wavelength_nm": [450.0, 550.0, 650.0],
            "largest_exact_nonzero_centered_kernel_row_col": [101, 101],
            "sensor_pixel_pitch_um": 1.0,
        }
    native = _matched_angular_edge(cases, images, masks, comparison)
    stage = evaluate_common_grid_edge(
        cases, images, masks, comparison, native, isp_profile=profile,
    )["model_input_support_diagnostic"]
    assert stage["all_recorded_global_psf_downstream_upper_bounds_fit_scene"] is False
    assert all(
        item["executed_psf_pixel_source_scene_halo"][
            "recorded_global_upper_bound_fits_source_scene"
        ]
        is False
        and item["executed_psf_pixel_source_scene_halo"][
            "recorded_global_upper_bound_margin_samples_row_col"
        ][0] < 0
        and item["executed_psf_pixel_source_scene_halo"][
            "scored_local_input_support_verified"
        ] is None
        for item in stage["per_slot"].values()
    )
    assert stage["physical_mtf_comparison_allowed"] is False


def test_short_global_psf_still_cannot_certify_unidentified_isp_halo():
    cases, images, masks, comparison = _angular_edge_fixture()
    for case in cases.values():
        case["conditions"]["sensor_pixel_pitch_um"] = 0.25
        case["conditions"]["model_psf_source_support"] = {
            "status": "global_exact_nonzero_bound_recorded",
            "scene_samples_row_col": [128, 128],
            "oi_sample_spacing_um": 0.25,
            "executed_wavelength_nm": [450.0, 550.0, 650.0],
            "largest_exact_nonzero_centered_kernel_row_col": [3, 3],
            "sensor_pixel_pitch_um": 0.25,
        }
    native = _matched_angular_edge(cases, images, masks, comparison)
    support = evaluate_common_grid_edge(
        cases, images, masks, comparison, native,
    )["model_input_support_diagnostic"]
    assert support["all_recorded_global_psf_downstream_upper_bounds_fit_scene"] is True
    assert all(
        item["executed_psf_pixel_source_scene_halo"][
            "recorded_global_upper_bound_fits_source_scene"
        ] is True
        and item["executed_psf_pixel_source_scene_halo"][
            "scored_local_input_support_verified"
        ] is None
        for item in support["per_slot"].values()
    )


def test_executed_psf_metadata_must_match_scene_grid_and_wavelengths():
    fov = 0.12
    spacing = 2 * 0.035 * math.tan(math.radians(fov) / 2) * 1e6 / 64
    resource = {
        "scope": "central_bounded_strict_raytrace",
        "scene_samples_row": 64, "scene_samples_col": 64,
        "scene_sample_spacing_um": spacing,
        "resampled_kernel_samples_row_col": [101, 101],
    }
    report = {"resource_preflight": resource, "preview": {"resource_preflight": resource}}
    scene = {
        "wavelength_nm": [450.0, 550.0, 650.0],
        "angular_grid_contract": {
            "source_shape": [64, 64], "horizontal_fov_deg": fov,
        },
    }
    lens = {"psf_execution": {
        "mode": "strict_bounded_streaming",
        "wavelength_nm": [450.0, 550.0, 650.0],
        "kernel_shape_row_col": [101, 101],
        "largest_exact_nonzero_centered_kernel_row_col": [91, 91],
    }}
    native = {"image_distance_m": 0.035}
    assert _executed_psf_source_support(
        report, scene, lens, native, 1.0,
    )["largest_exact_nonzero_centered_kernel_row_col"] == [91, 91]
    changed = deepcopy(lens)
    changed["psf_execution"]["wavelength_nm"] = [450.0, 550.0]
    with pytest.raises(ValueError, match="wavelength/kernel axes"):
        _executed_psf_source_support(report, scene, changed, native, 1.0)

def test_scored_boundary_check_fails_when_interpolation_needs_frame_extension():
    cases, images, masks, _ = _angular_edge_fixture()
    native = {
        "available": True,
        "per_slot": {
            slot: {"native_roi_row_col_exclusive": [[0, 32], [0, 32]]}
            for slot in SLOTS
        },
    }
    grid = {
        "shape_row_col": [16, 16],
        "grid_geometry": "uniform_central_square",
        "row_angle_deg": [-0.002, 0.018],
        "column_angle_deg": [-0.002, 0.018],
        "step_deg_row_col": [0.02 / 15, 0.02 / 15],
    }
    stage = _model_input_support_diagnostic(
        cases, images, masks, native, {"grid": grid}, None,
    )
    assert stage["available"] is True
    assert stage["all_scored_resampling_boundaries_independent"] is False
    assert stage["per_slot"]["L1S1"]["scored_vertex_min_native_frame_border_margin_pixels"] < max(
        stage["per_slot"]["L1S1"]["scored_antialias_radius_native_pixels_row_col"]
    )
    assert stage["physical_mtf_comparison_allowed"] is False


@pytest.mark.parametrize("also_omit_old_proxy", [False, True])
def test_historical_absence_preserves_metric_and_present_diagnostic_checks(also_omit_old_proxy):
    _, current = _evaluate()
    historical = deepcopy(current)
    del historical["physical_mtf_comparability"]
    del historical["model_input_support_diagnostic"]
    if also_omit_old_proxy:
        del historical["model_550nm_point_cell_footprint"]
    assert _saved_shared_metric_matches(historical, current)
    with_support = deepcopy(current)
    old_support = with_support["model_input_support_diagnostic"]
    old_support.pop("all_recorded_global_psf_downstream_upper_bounds_fit_scene")
    for item in old_support["per_slot"].values():
        item.pop("executed_psf_pixel_source_scene_halo")
    assert _saved_shared_metric_matches(with_support, current)
    historical["pairs"][0]["right_minus_left_cycles_per_degree_object_angle"] += 1
    assert not _saved_shared_metric_matches(historical, current)
    changed = deepcopy(current)
    changed["physical_mtf_comparability"]["physical_mtf_comparison_allowed"] = True
    assert not _saved_shared_metric_matches(changed, current)
    changed["physical_mtf_comparability"] = None
    assert not _saved_shared_metric_matches(changed, current)
    changed = deepcopy(current)
    changed["model_input_support_diagnostic"]["source_scene_halo_verified"] = True
    assert not _saved_shared_metric_matches(changed, current)


def test_job_revalidates_support_diagnostic_even_if_metric_unavailable(tmp_path, monkeypatch):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    queued = submit(service, _matrix(service, monkeypatch))
    assert service.registry.claim()["id"] == queued["id"]
    service.registry.finish(
        queued["id"], result=execute(service, service.registry.get_run(queued["id"]))
    )
    sealed = get(service, queued["id"])
    saved_metric = sealed["result"]["summary"]["comparison"]["common_grid_object_angle_edge_mtf50"]
    assert saved_metric["physical_mtf_comparability"]["product_ranking_allowed"] is False
    assert all(
        case["isp_implementation"] == isp_v2.IMPLEMENTATION
        and case["isp_crop_full_frame_halo_native_pixels"] == 1
        for case in sealed["result"]["summary"]["cases"].values()
    )
    original_get = service.registry.get_run

    def reseal(record):
        report = service.registry.store_blob(
            canonical(record["result"]["summary"]).encode(),
            "lens-sensor-quality-study-result.json", "application/json",
        )
        record["artifacts"] = [
            item for item in record["artifacts"]
            if item["name"] != "lens-sensor-quality-study-result.json"
        ] + [report]
        record["result"]["artifacts"] = record["artifacts"]
        monkeypatch.setattr(
            service.registry, "get_run",
            lambda identity: record if identity == queued["id"] else original_get(identity),
        )

    historical = deepcopy(sealed)
    del historical["result"]["summary"]["comparison"]["common_grid_object_angle_edge_mtf50"][
        "physical_mtf_comparability"
    ]
    reseal(historical)
    assert "physical_mtf_comparability" not in get(service, queued["id"])["result"]["summary"][
        "comparison"
    ]["common_grid_object_angle_edge_mtf50"]

    tampered = deepcopy(sealed)
    tampered["result"]["summary"]["comparison"]["common_grid_object_angle_edge_mtf50"][
        "physical_mtf_comparability"
    ]["physical_mtf_comparison_allowed"] = True
    reseal(tampered)
    with pytest.raises(IntegrityError, match="common-grid"):
        get(service, queued["id"])

    tampered_halo = deepcopy(sealed)
    tampered_halo["result"]["summary"]["cases"]["L1S1"][
        "isp_crop_full_frame_halo_native_pixels"
    ] = 0
    reseal(tampered_halo)
    with pytest.raises(IntegrityError, match="ISP identity or halo"):
        get(service, queued["id"])

    tampered_implementation = deepcopy(sealed)
    tampered_implementation["result"]["summary"]["cases"]["L1S1"][
        "isp_implementation"
    ] = None
    reseal(tampered_implementation)
    with pytest.raises(IntegrityError, match="ISP identity or halo"):
        get(service, queued["id"])

    tampered_stage = deepcopy(sealed)
    tampered_stage["result"]["summary"]["comparison"][
        "common_grid_object_angle_edge_mtf50"
    ]["model_input_support_diagnostic"]["optical_psf_input_support_verified"] = True
    reseal(tampered_stage)
    with pytest.raises(IntegrityError, match="common-grid"):
        get(service, queued["id"])


def test_new_quality_job_sharpen_profile_binds_two_pixel_isp_halo(tmp_path, monkeypatch):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    spec = _matrix(service, monkeypatch)
    spec["profile"]["linear_sharpen"]["mode"] = "bounded_detail_v1"
    queued = submit(service, spec)
    assert service.registry.claim()["id"] == queued["id"]
    service.registry.finish(
        queued["id"], result=execute(service, service.registry.get_run(queued["id"]))
    )
    summary = get(service, queued["id"])["result"]["summary"]
    assert all(
        case["isp_implementation"] == isp_v2.IMPLEMENTATION
        and case["isp_crop_full_frame_halo_native_pixels"] == 2
        for case in summary["cases"].values()
    )


def _same_sampling_preflight_fixture(psf_side=101):
    cases = {}
    reports = {}
    stage = {"available": True, "per_slot": {}}
    geometry = {}
    limits = {}
    for slot in SLOTS:
        lens_id = slot[:2]
        revision = f"revision-{lens_id}"
        resource = {
            "scope": "central_bounded_strict_raytrace",
            "scene_samples_row": 64,
            "scene_samples_col": 64,
            "scene_sample_spacing_um": 0.25,
            "resampled_kernel_samples_row_col": [psf_side + 20] * 2,
            "estimated_bounded_working_gib": 0.1,
        }
        cases[slot] = {
            "source_job_id": f"source-{slot}",
            "conditions": {
                "sensor_size": [32, 32],
                "angular_scene_grid_contract": {
                    "horizontal_fov_deg": 0.12,
                    "vertical_fov_deg": 0.12,
                },
                "native_output_angular_grid": {
                    "distortion_aware_point_center": {
                        "input_scene_support": {
                            "status": "all_model_point_centers_within_source_samples",
                            "max_abs_input_angle_deg_row_col_by_wavelength": (
                                [[0.01, 0.01]] * 3
                            ),
                        }
                    }
                },
                "model_psf_source_support": {
                    "status": "global_exact_nonzero_bound_recorded",
                    "scene_samples_row_col": [64, 64],
                    "oi_sample_spacing_um": 0.25,
                    "sensor_pixel_pitch_um": 0.5,
                    "executed_wavelength_nm": [450.0, 550.0, 650.0],
                    "resampled_kernel_samples_row_col": [psf_side + 20] * 2,
                    "largest_exact_nonzero_centered_kernel_row_col": [psf_side] * 2,
                },
            },
        }
        reports[slot] = {
            "resource_preflight": resource,
            "source_identities": {"lens": {"revision_id": revision}},
            "specification": {"lens_revision_id": revision},
            "preview": {"lens_model": {"psf_execution": {
                "nonzero_input_convolutions": 12,
            }}},
        }
        stage["per_slot"][slot] = {
            "scored_antialias_radius_native_pixels_row_col": [0, 0],
            "executed_psf_pixel_source_scene_halo": {
                "status": "global_bound_evaluated",
                "scene_samples_row_col": [64, 64],
                "executed_global_max_nonzero_psf_radius_samples_row_col": [
                    (psf_side - 1) / 2,
                ] * 2,
                "fixed_isp_radius_native_pixels": None,
            },
        }
        geometry[revision] = {
            str(wave): {
                "ideal_image_height_mm": [0.0, 1.0],
                "distorted_image_height_mm": [0.0, 1.0],
            }
            for wave in (450, 550, 650)
        }
        limits[revision] = 90.0
    return cases, stage, reports, geometry, limits


def test_same_sampling_preflight_refuses_a_window_above_raw_schema_cap():
    cases, stage, reports, geometry, limits = _same_sampling_preflight_fixture(901)
    plan = plan_same_sampling_mtf_recapture(
        cases, stage, reports,
        strict_geometry_by_lens_revision=geometry,
        max_fov_deg_by_lens_revision=limits,
    )
    assert plan["provisional_power_of_two_scene_samples_row_col"] == [1024, 1024]
    assert plan["can_submit_provisional_scene_through_current_raw_contract"] is False
    assert math.isclose(
        math.tan(math.radians(plan["provisional_same_sampling_horizontal_fov_deg"]) / 2)
        / 1024,
        math.tan(math.radians(0.12) / 2) / 64,
        rel_tol=1e-12,
    )
    with pytest.raises(ValueError, match="same_sampling_scene_unsupported"):
        require_supported_same_sampling_recapture(plan)
    with pytest.raises(ValueError, match="through 256"):
        _scene_samples((1024, 1024), (32, 32))
    assert plan["physical_mtf_comparison_allowed"] is False
    assert plan["product_ranking_allowed"] is False


def test_even_if_window_fits_mosaic_operator_is_not_silently_certified():
    cases, stage, reports, geometry, limits = _same_sampling_preflight_fixture(101)
    plan = plan_same_sampling_mtf_recapture(
        cases, stage, reports,
        strict_geometry_by_lens_revision=geometry,
        max_fov_deg_by_lens_revision=limits,
    )
    assert plan["can_submit_provisional_scene_through_current_raw_contract"] is True
    assert plan["executed_mosaic_operator_support_recorded"] is False
    assert all(
        item["conditional_default_mosaic_operator"]["credited_to_source_halo_or_product_mtf"]
        is False
        for item in plan["per_slot"].values()
    )
    with pytest.raises(ValueError, match="mosaic_operator_unrecorded"):
        require_supported_same_sampling_recapture(plan)


def test_zero_absolute_input_angle_axis_is_valid_but_not_mosaic_evidence():
    cases, stage, reports, geometry, limits = _same_sampling_preflight_fixture(101)
    for case in cases.values():
        case["conditions"]["native_output_angular_grid"][
            "distortion_aware_point_center"
        ]["input_scene_support"]["max_abs_input_angle_deg_row_col_by_wavelength"] = (
            [[0.0, 0.01]] * 3
        )
    plan = plan_same_sampling_mtf_recapture(
        cases, stage, reports,
        strict_geometry_by_lens_revision=geometry,
        max_fov_deg_by_lens_revision=limits,
    )
    assert plan["per_slot"]["L1S1"][
        "all_wavelength_native_frame_max_center_offset_oi_samples_row_col"
    ][0] == 0.0
    assert plan["executed_mosaic_operator_support_recorded"] is False


def test_strict_distortion_inverse_factor_uses_piecewise_segment_extrema():
    axes = {
        "ideal_image_height_mm": [0.0, 1.0, 2.0],
        "distorted_image_height_mm": [0.0, 0.5, 2.0],
    }
    assert _strict_geometry_max_inverse_factor(axes, 0.25) == 2.0
    assert _strict_geometry_max_inverse_factor(axes, 1.5) == 2.0
    with pytest.raises(ValueError, match="increase from zero"):
        _strict_geometry_max_inverse_factor({
            **axes, "distorted_image_height_mm": [0.0, 0.5, 0.4],
        }, 1.0)


def test_even_mosaic_gaussian_uses_integer_convolution_offset_bound():
    operator = _conditional_single_sample_mosaic_radius_samples(0.7, 0.20234547859227803)
    assert operator["gaussian_kernel_samples_row_col"] == [4, 4]
    assert operator["gaussian_kernel_radius_samples_row_col"] == [2, 2]
    assert operator["combined_radius_bound_samples_row_col"] == [3.0, 3.0]
    assert operator["credited_to_source_halo_or_product_mtf"] is False
