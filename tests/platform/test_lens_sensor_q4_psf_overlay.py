"""q4 overlay is opt-in and changes only physical PSF grid and asset identity."""

from __future__ import annotations

import copy

import numpy as np
import pytest

from camera_platform.lens_dense_geometric_psf_q4_rebin import SCHEMA, STATUS
from camera_platform.lens_geometric_psf_rehistogram import _array_sha
from camera_platform.lens_sensor_q4_psf_overlay import (
    optics_with_q4_geometric_psf_candidate,
)
from pyisetcam.optics import _raytrace_psf_support_axes, validate_raytrace_contract


def _fixture():
    # An even source side has a half-sample optical center. q4 is odd but has
    # the same physical sample-center support in both axes.
    source_psf = np.zeros((8, 8, 2, 1), dtype=np.float64)
    source_psf[3, 3, :, 0] = 0.5
    source_psf[4, 4, :, 0] = 0.5
    q4_psf = np.zeros((29, 29, 2, 1), dtype=np.float32)
    q4_psf[14, 14, 0, 0] = 1.0
    q4_psf[13, 14, 1, 0] = 0.25
    q4_psf[14, 14, 1, 0] = 0.75
    fields = np.asarray([0.0, 1.0])
    waves = np.asarray([550.0])
    pitch = np.asarray([0.001, 0.001])
    source = {
        "model": "raytrace",
        "focal_length_m": 0.004,
        "lens_patent_raytrace_psf": {"simulation_id": "simulation-a"},
        "raytrace": {
            "contract_version": 2,
            "physics_policy": "strict",
            "psf_kind": "geometric",
            "diffraction_included": False,
            "asset_sha256": "a" * 64,
            "source_prescription_sha256": "e" * 64,
            "object_distance_m": float("inf"),
            "geometry": {
                "function": np.asarray([[0.0], [1.0]]),
                "field_height_mm": fields,
                "wavelength_nm": waves,
            },
            "relative_illumination": {
                "function": np.ones((2, 1)),
                "field_height_mm": fields,
                "wavelength_nm": waves,
            },
            "chief_ray_angle_deg": np.zeros((2, 1)),
            "image_direction_cosines": np.ones((2, 1, 3)),
            "psf": {
                "function": source_psf,
                "field_height_mm": fields,
                "wavelength_nm": waves,
                "sample_spacing_mm": pitch,
                "coordinate_convention": "pixel_centers",
                "field_coordinate": "ideal_image_height",
            },
        },
    }
    report = {
        "schema": SCHEMA,
        "status": STATUS,
        "candidate_asset_sha256": "b" * 64,
        "parent_candidate_run_id": "run-" + "1" * 32,
        "parent_candidate_asset_sha256": "c" * 64,
        "parent_candidate_psf_array_sha256": "d" * 64,
        "parent_dense_intersections_array_sha256": "f" * 64,
        "source_psf_asset_sha256": "a" * 64,
        "source_prescription_sha256": "e" * 64,
        "lens_revision_content_sha256": "4" * 64,
        "lens_db_sha256": "5" * 64,
        "simulation_id": "simulation-a",
        "same_parent_dense_rays_reused_without_raytrace": True,
        "all_stored_field_wavelength_nodes_completed": True,
        "histogram_refinement_factor": 4,
        "optical_psf_semantics": "geometric_pre_pixel_ray_histogram",
        "diffraction_included": False,
        "pixel_aperture_included": False,
        "pupil_density_convergence_qualified_all_nodes": False,
        "scene_or_executed_convolution_sampling_qualified": False,
        "lens_radiometry_validated": False,
        "system_mtf_or_product_ranking_allowed": False,
        "maximum_histogram_complex_otf_abs_delta_allowed": 0.01,
        "q4_worst_complex_otf_abs_gap": 0.003,
        "histogram_gap_within_declared_tolerance_model_only": True,
        "histogram_target_status": "numeric_gap_within_declared_tolerance_model_only",
        "source_psf_shape_row_col_field_wave": [8, 8, 2, 1],
        "parent_q2_psf_shape_row_col_field_wave": [15, 15, 2, 1],
        "candidate_psf_shape_row_col_field_wave": [29, 29, 2, 1],
        "source_psf_array_sha256": _array_sha(source_psf, "<f4"),
        "candidate_psf_array_sha256": _array_sha(q4_psf, "<f4"),
        "field_height_mm": fields.tolist(),
        "wavelength_nm": waves.tolist(),
        "source_histogram_pitch_mm_row_col": pitch.tolist(),
        "parent_q2_histogram_pitch_mm_row_col": (pitch / 2).tolist(),
        "candidate_histogram_pitch_mm_row_col": (pitch / 4).tolist(),
        "physical_sample_center_support_mm_row_col": [0.007, 0.007],
        "node_count": 2,
        "output_psf_float32_bytes": q4_psf.nbytes,
    }
    arrays = {
        "psf_function": q4_psf,
        "field_height_mm": fields.copy(),
        "wavelength_nm": waves.copy(),
        "source_sample_spacing_mm": pitch.copy(),
        "sample_spacing_mm": pitch / 4,
    }
    return source, report, arrays


def test_overlay_preserves_even_source_support_and_other_optics():
    source, report, arrays = _fixture()
    validate_raytrace_contract(source)
    source_before = copy.deepcopy(source)
    selected = optics_with_q4_geometric_psf_candidate(source, report, arrays)
    validate_raytrace_contract(selected)
    ray = selected["raytrace"]
    assert ray["asset_sha256"] == "b" * 64
    assert ray["source_psf_asset_sha256"] == "a" * 64
    assert ray["q4_geometric_psf_candidate"]["product_ranking_allowed"] is False
    assert ray["q4_geometric_psf_candidate"][
        "scene_or_executed_convolution_sampling_qualified"
    ] is False
    assert ray["geometry"] is source["raytrace"]["geometry"]
    assert ray["relative_illumination"] is source["raytrace"]["relative_illumination"]
    assert ray["chief_ray_angle_deg"] is source["raytrace"]["chief_ray_angle_deg"]
    assert ray["image_direction_cosines"] is source["raytrace"]["image_direction_cosines"]
    assert selected["focal_length_m"] == source["focal_length_m"]
    assert source["raytrace"]["asset_sha256"] == source_before["raytrace"]["asset_sha256"]
    np.testing.assert_array_equal(
        source["raytrace"]["psf"]["function"],
        source_before["raytrace"]["psf"]["function"],
    )
    np.testing.assert_array_equal(ray["psf"]["sample_spacing_mm"], [0.00025, 0.00025])
    assert ray["psf"]["function"].shape == (29, 29, 2, 1)
    source_x, source_y = _raytrace_psf_support_axes(source["raytrace"]["psf"])
    q4_x, q4_y = _raytrace_psf_support_axes(ray["psf"])
    np.testing.assert_allclose([source_x[0], source_x[-1], source_y[0], source_y[-1]],
                               [q4_x[0], q4_x[-1], q4_y[0], q4_y[-1]], atol=1e-15)
    arrays["psf_function"][14, 14, 0, 0] = 0
    arrays["sample_spacing_mm"][0] = 1
    assert ray["psf"]["function"][14, 14, 0, 0] == 1
    assert ray["psf"]["sample_spacing_mm"][0] == 0.00025
    assert ray["psf"]["function"].flags.writeable is False


@pytest.mark.parametrize("change, match", [
    (lambda _source, report, _arrays: report.update(
        parent_candidate_asset_sha256="not-a-sha"), "lineage"),
    (lambda _source, report, _arrays: report.update(
        source_psf_asset_sha256="0" * 64), "lineage"),
    (lambda source, _report, _arrays: source["lens_patent_raytrace_psf"].update(
        simulation_id="different"), "lineage"),
    (lambda _source, report, _arrays: report.update(
        source_psf_array_sha256="0" * 64), "Source Lens PSF"),
    (lambda _source, report, _arrays: report.update(
        parent_q2_psf_shape_row_col_field_wave=[16, 16, 2, 1]), "pitch"),
    (lambda _source, report, _arrays: report.update(
        physical_sample_center_support_mm_row_col=[0.008, 0.007]), "support"),
    (lambda _source, report, _arrays: report.update(
        histogram_gap_within_declared_tolerance_model_only=False), "target"),
    (lambda _source, _report, arrays: arrays.update(
        sample_spacing_mm=np.asarray([0.00025, 0.0003])), "pitch"),
    (lambda _source, _report, arrays: arrays["psf_function"].__setitem__(
        (14, 14, 0, 0), 0.5), "mass"),
    (lambda source, _report, _arrays: source["raytrace"]["psf"].update(
        coordinate_convention="legacy"), "strict"),
])
def test_overlay_rejects_wrong_pitch_lineage_or_contract(change, match):
    source, report, arrays = _fixture()
    change(source, report, arrays)
    with pytest.raises(ValueError, match=match):
        optics_with_q4_geometric_psf_candidate(source, report, arrays)
