"""Source-bound 2 x 2 quality diagnostics never turn unmatched RAWs into rankings."""

from __future__ import annotations

import io
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from scipy.special import erf

from camera_platform.api import create_app
from camera_platform.lens_sensor_angular_geometry import strict_raytrace_pixel_center_angles
from camera_platform.lens_sensor_common_angular_mtf import (
    _model_point_cell_footprint,
    evaluate_common_grid_edge,
)
from camera_platform.lens_sensor_quality_job import (
    _inputs,
    _normalize_dev28_comparison_wording,
    _saved_shared_metric_matches,
    default_spec,
    execute,
    get,
    prepare,
    specification,
    submit,
)
from camera_platform.lens_sensor_quality_study import (
    SCHEMA,
    SCHEMA_V2,
    _comparison,
    _matched_angular_edge,
    evaluate_matrix,
)
from camera_platform.registry import IntegrityError, canonical, digest
from camera_platform.retained_raw_dataset import load_retained_lens_sensor_raw
from camera_platform.sdk import PlatformClient
from camera_platform.service import PlatformService
from pyisetcam.dataset import _sha256_array

WORKSPACE = Path(__file__).resolve().parents[2]
SLOTS = ("L1S1", "L1S2", "L2S1", "L2S2")
FOV = (0.01357298071438191, 0.015575551616375409,
       0.005308925389059417, 0.006092209461473999)


def _source_job(
    service: PlatformService, *, slot: str, fov: float, size: int = 8,
    exposure: float = 0.004, name_suffix: str = "",
) -> str:
    lens = slot[:2]
    sensor = slot[2:]
    y, x = np.indices((size, size))
    raw = (64 + 80 * (x + 0.17 * y > size / 2) + (1 if lens == "L2" else 0)
           + (2 if sensor == "S2" else 0)).astype(np.int32)
    stream = io.BytesIO()
    np.save(stream, raw, allow_pickle=False)
    raw_descriptor = service.registry.store_blob(
        stream.getvalue(), "lens-sensor-original-raw.npy", "application/octet-stream"
    )
    raw_descriptor.update(dtype="int32", shape=[size, size], unit="DN",
                          source_kind="simulated")
    spec = {
        "schema": "camerae2e_lens_sensor_raw_preview_study_v1",
        "lens_revision_id": f"lens-{lens}",
        "sensor_revision": {"identifier": f"sensor-{sensor}", "run_id": f"run-{sensor}"},
        "scene_kind": "slanted_edge", "sensor_size": [size, size],
        "luminance_cd_m2": 1000.0, "exposure_s": exposure, "noise_flag": 0,
        "seed": 7, "accept_approximate_lens_radiometry": True,
    }
    identities = {
        "lens": {"revision_id": f"lens-{lens}", "asset_sha256": lens.lower() * 32},
        "sensor": {"run_id": f"run-{sensor}", "result_sha256": sensor.lower() * 32},
    }
    resource = {"scope": "central_full_raytrace"}
    request = {
        "name": f"saved {slot}{name_suffix}", "job_kind": "lens_sensor_raw",
        "plugin_id": spec["schema"], "specification": spec,
        "source_identities": identities,
        "upstream_assets": {"digest": "offline-assets-1"},
        "resource_preflight": resource,
    }
    queued = service.registry.submit(request, digest(request))
    assert service.registry.claim()["id"] == queued["id"]
    summary = {
        "schema": spec["schema"], "status": "completed", "specification": spec,
        "source_identities": identities,
        "upstream_assets": request["upstream_assets"],
        "resource_preflight": resource,
        "preview": {
            "resource_preflight": resource, "raw_artifact": raw_descriptor,
            "raw": {"dtype": "int32", "shape": [size, size], "values": raw.tolist()},
            "physical_validation": "not_established",
            "capture": {"seed": 7, "sensor_size": [size, size], "exposure_s": exposure,
                        "noise_flag": 0, "saturation": {
                            "adc_white_fraction": 0.0, "adc_zero_fraction": 0.0,
                            "well_fraction": 0.0}},
            "lens_identity": identities["lens"], "sensor_revision": identities["sensor"],
            "scope": "approximate_geometric_central_field_raw_preview",
            "lens_model": {
                "field_psf_selection": {"mode": "centered_scene_full_raytrace"},
                "absolute_radiometry": "unknown_unity_transmission_assumption",
                "relative_illumination": "unknown_unity_assumption",
                "psf_sampling_validation": "not_established",
                "focal_length_m": 0.035 if lens == "L1" else 0.025,
                "f_number": 4.0,
            },
            "sensor_model": {"pixel_pitch_um": 1.2 if sensor == "S1" else 1.4},
            "scene": {
                "kind": "slanted_edge_d65", "generator": "pyisetcam.sensor_comparison._make_scene",
                "geometry": {"scene_to_crop_scale": 1.2},
                "horizontal_fov_deg": fov, "luminance_cd_m2": 1000.0,
                "object_distance_m": "infinity", "wavelength_nm": [450.0, 550.0, 650.0],
            },
        },
    }
    result_descriptor = service.registry.store_blob(
        canonical(summary).encode(), "lens-sensor-raw-preview-result.json", "application/json"
    )
    service.registry.finish(
        queued["id"], result={"summary": summary,
                              "artifacts": [raw_descriptor, result_descriptor]}
    )
    return queued["id"]


def _fake_inspect(report_path, raw_path, *, source_lens_sensor_job_id,
                  sensor_db_root, profile):
    _, raw, binding = load_retained_lens_sensor_raw(
        report_path, raw_path, source_lens_sensor_job_id=source_lens_sensor_job_id
    )
    return {
        "raw": raw, "source_binding": binding,
        "raw_content_sha256": _sha256_array(raw),
        "native_cfa_contract": "three_channel_rggb_2x2",
        "raw_format": {
            "cfa": "RGGB", "bit_depth": 10, "black_level_dn": 64,
            "response_type": "linear", "packing": "unpacked", "layout": "bayer",
            "hdr_streams": 1,
        },
        "geometry": {"kind": "native_sensor", "full_sensor_shape": list(raw.shape),
                     "crop_origin": [0, 0]},
        "profile": profile,
    }


def _matrix(service, monkeypatch, *, size=8, fovs=FOV, exposures=None):
    monkeypatch.setattr(
        "camera_platform.retained_raw_isp_bridge.inspect_lens_sensor_isp_input",
        _fake_inspect,
    )
    ids = {
        slot: _source_job(
            service, slot=slot, fov=fovs[index], size=size,
            exposure=(exposures or [0.004] * 4)[index],
        )
        for index, slot in enumerate(SLOTS)
    }
    spec = default_spec()
    spec.update(source_jobs=ids, acknowledge_simulation_limits=True)
    return spec


def test_stored_dev9_fov_pattern_is_incomparable_and_8px_mtf_unavailable(tmp_path, monkeypatch):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    spec = _matrix(service, monkeypatch)
    frozen = prepare(service, spec)
    report, arrays = evaluate_matrix(_inputs(service, frozen["specification"]),
                                     profile=frozen["specification"]["profile"])
    comparison = report["comparison"]
    assert comparison["horizontal_fov_deg_by_slot"] == dict(zip(SLOTS, FOV, strict=True))
    assert comparison["matched_source_controls"] is True
    assert comparison["matched_horizontal_fov"] is False
    assert comparison["edge_mtf50_diagnostic_delta_allowed"] is False
    assert comparison["product_ranking_allowed"] is False
    assert all("right_minus_left_cycles_per_output_pixel" not in pair
               for pair in comparison["pairwise_diagnostic_deltas"])
    for slot in SLOTS:
        assert report["cases"][slot]["edge_mtf50"]["available"] is False
        assert "too small" in report["cases"][slot]["edge_mtf50"]["reason"]
        assert report["cases"][slot]["temporal_snr_db"]["available"] is False
        assert "independent seeds" in report["cases"][slot]["temporal_snr_db"]["reason"]
        assert arrays[f"{slot}_raw_dn"].dtype == np.int32


def test_32px_matched_input_fov_still_lacks_native_output_angular_grid(
    tmp_path, monkeypatch,
):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    spec = _matrix(service, monkeypatch, size=32, fovs=[0.04] * 4)
    monkeypatch.setattr(
        "camera_platform.lens_sensor_quality_study._edge_metrics",
        lambda rgb: {"available": True, "mtf50_cycles_per_output_pixel":
                     float(np.mean(rgb)), "method": "test_edge_metric"},
    )
    report, _ = evaluate_matrix(_inputs(service, spec), profile=spec["profile"])
    comparison = report["comparison"]
    assert comparison["matched_horizontal_fov"] is True
    assert comparison["matched_native_output_angular_grid"] is False
    assert comparison["edge_mtf50_diagnostic_delta_allowed"] is False
    assert comparison["product_ranking_allowed"] is False
    assert all(not pair["edge_mtf50_delta_available"] for pair in
               comparison["pairwise_diagnostic_deltas"])
    assert all(report["cases"][slot]["edge_mtf50"]["available"] for slot in SLOTS)


def test_v4_same_input_scene_never_opens_numeric_delta_with_center_only_output_grid():
    common = {
        "source_schema": "camerae2e_lens_sensor_raw_preview_study_v4",
        "scene_kind": "slanted_edge_d65",
        "scene_generator": "pyisetcam.sensor_comparison._make_scene",
        "scene_geometry": {"native_crop_scale": "depends_on_executed_lens_and_sensor"},
        "angular_scene_grid_contract": {
            "mode": "explicit_common_input_scene",
            "source_shape": [64, 64],
            "horizontal_fov_deg": 0.12,
            "vertical_fov_deg": 0.12,
            "center_angular_step_deg_row_col": [0.001875, 0.001875],
            "native_output_grid_alignment": "not_established",
        },
        "scene_wavelength_nm": [450.0, 550.0, 650.0],
        "luminance_cd_m2": 1000.0,
        "object_distance_m": "infinity",
        "horizontal_fov_deg": 0.12,
        "sensor_size": [32, 32],
        "exposure_s": 0.004,
        "noise_flag": 0,
        "seed": 7,
        "lens_scope": "approximate_geometric_central_field_raw_preview",
        "field_psf_mode": "centered_scene_full_raytrace",
        "psf_compute_mode": "bounded_streaming",
        "native_cfa_contract": "three_channel_rggb_2x2",
        "upstream_assets": {"digest": "same"},
    }
    cases = {
        slot: {
            "conditions": {
                **common,
                "native_output_angular_grid": {
                    "status": "paraxial_center_only_full_distortion_map_not_established",
                    "center_angular_step_deg_row_col": [0.001 * (index + 1)] * 2,
                },
            },
            "edge_mtf50": {"available": True, "value": float(index)},
        }
        for index, slot in enumerate(SLOTS)
    }
    result = _comparison(cases)
    assert result["matched_horizontal_fov"] is True
    assert result["matched_native_output_angular_grid"] is False
    assert result["input_scene_coarser_than_native_output_by_slot"] == {
        "L1S1": True, "L1S2": False, "L2S1": False, "L2S2": False,
    }
    assert any("source sampling may limit" in reason for reason in result["reasons"])
    assert result["edge_mtf50_diagnostic_delta_allowed"] is False
    assert all("right_minus_left_cycles_per_output_pixel" not in pair
               for pair in result["pairwise_diagnostic_deltas"])


def test_distortion_aware_point_maps_expose_overlap_but_not_a_comparable_mtf_grid():
    raytrace = {
        "physics_policy": "strict",
        "geometry": {
            "field_coordinate": "ideal_image_height",
            "field_height_mm": [0.0, 1.0],
            "wavelength_nm": [450.0, 550.0, 650.0],
            "function": [[0.0, 0.0, 0.0], [0.9, 1.0, 1.1]],
        },
    }
    common = {
        "source_schema": "camerae2e_lens_sensor_raw_preview_study_v4",
        "scene_kind": "slanted_edge_d65", "scene_generator": "test",
        "scene_geometry": {}, "scene_wavelength_nm": [450.0, 550.0, 650.0],
        "luminance_cd_m2": 1000.0, "object_distance_m": "infinity",
        "horizontal_fov_deg": 0.12, "sensor_size": [8, 8],
        "exposure_s": 0.004, "noise_flag": 0, "seed": 7,
        "lens_scope": "approximate_geometric_central_field_raw_preview",
        "field_psf_mode": "centered_scene_full_raytrace",
        "psf_compute_mode": "bounded_streaming",
        "native_cfa_contract": "three_channel_rggb_2x2",
        "upstream_assets": {"digest": "same"},
        "angular_scene_grid_contract": {
            "mode": "explicit_common_input_scene",
            "source_shape": [22, 22], "horizontal_fov_deg": 0.12,
            "center_angular_step_deg_row_col": [0.12 / 22, 0.12 / 22],
            "native_output_grid_alignment": "not_established",
        },
    }
    cases = {}
    for index, slot in enumerate(SLOTS):
        pitch = (1.0 + 0.1 * index) * 1e-6
        point = strict_raytrace_pixel_center_angles(
            raytrace, sensor_shape=(8, 8),
            sensor_sample_size_m_row_col=(pitch, pitch),
            image_distance_m=0.01, wavelength_nm=(450.0, 550.0, 650.0),
        )
        coordinates = np.asarray(point["coordinates_deg_row_col_by_wavelength"])
        point["input_scene_support"] = {
            "status": "all_model_point_centers_within_source_samples",
            "max_abs_input_angle_deg_row_col_by_wavelength": np.max(
                np.abs(coordinates), axis=(1, 2)
            ).tolist(),
            "source_sample_center_support_deg_row_col": [0.06, 0.06],
        }
        cases[slot] = {
            "conditions": {
                **common,
                "native_output_angular_grid": {
                    "status": "paraxial_center_only_full_distortion_map_not_established",
                    "pixel_pitch_m_row_col": [pitch, pitch],
                    "image_distance_m": 0.01,
                    "center_angular_step_deg_row_col": [
                        float(np.degrees(np.arctan(pitch / 0.01)))
                    ] * 2,
                    "distortion_aware_point_center": point,
                },
            },
            "edge_mtf50": {"available": True, "value": 0.1},
        }
    result = _comparison(cases)
    assert all(result["distortion_aware_point_center_map_by_slot"].values())
    assert result["common_550nm_point_center_roi"]["nonempty"] is True
    assert all(
        count > 0 for count in result["common_550nm_point_center_roi"][
            "native_pixel_centers_in_overlap_by_slot"
        ].values()
    )
    assert all(value > 0 for value in result[
        "source_step_over_min_distorted_step_by_slot"
    ].values())
    # The first distorted-wavelength step is finer than the source, even
    # though the legacy paraxial step is coarser than the source.
    assert result["source_step_over_min_distorted_step_by_slot"]["L1S1"] > 1.0
    assert result["input_scene_coarser_than_native_output_by_slot"]["L1S1"] is True
    assert 0.12 / 22 < cases["L1S1"]["conditions"]["native_output_angular_grid"][
        "center_angular_step_deg_row_col"
    ][0]
    assert result["matched_native_output_angular_grid"] is False
    assert result["edge_mtf50_diagnostic_delta_allowed"] is False
    assert result["product_ranking_allowed"] is False
    assert any("PSF/pixel-integrated native RAW output footprints" in reason
               for reason in result["reasons"])
    assert all("angular ROI resampling" not in reason for reason in result["reasons"])
    assert "angular diagnostics are evaluated separately" in result["scope"]
    assert all("native cycles/output-pixel pairwise gate" in pair["reason"].lower()
               for pair in result["pairwise_diagnostic_deltas"])


def _angular_edge_fixture(steps=None):
    """The same object-angle Gaussian edge sampled at four native pitches."""
    steps = steps or dict(zip(SLOTS, (0.0010, 0.0011, 0.0012, 0.0013), strict=True))
    cases, images, masks = {}, {}, {}
    for slot, step in steps.items():
        yy, xx = np.indices((32, 32), dtype=float)
        row_angle, col_angle = (yy - 15.5) * step, (xx - 15.5) * step
        centers = np.stack((row_angle, col_angle), axis=-1)
        edge_distance = (col_angle - row_angle / 12.0) / np.hypot(1.0, 1.0 / 12.0)
        luma = 0.1 + 0.8 * (1.0 + erf(edge_distance / (np.sqrt(2.0) * 0.002))) / 2.0
        cases[slot] = {
            "conditions": {
                "source_schema": "camerae2e_lens_sensor_raw_preview_study_v5",
                "noise_flag": 0,
                "scene_wavelength_nm": [450.0, 550.0, 650.0],
                "native_output_angular_grid": {
                    "distortion_aware_point_center": {
                        "coordinates_deg_row_col_by_wavelength": np.stack(
                            (centers * 1.001, centers, centers * 0.999)
                        ).tolist(),
                    },
                },
            },
            "saturation_fractions": {
                "adc_white_fraction": 0.0,
                "adc_zero_fraction": 0.0,
                "well_fraction": 0.0,
            },
        }
        images[slot] = np.repeat(luma[..., None], 3, axis=-1)
        masks[slot] = np.ones((32, 32), dtype=bool)
    extent = 15.5 * steps["L1S1"]
    comparison = {
        "matched_source_controls": True,
        "matched_horizontal_fov": True,
        "distortion_aware_point_center_map_by_slot": {slot: True for slot in SLOTS},
        "source_step_over_min_distorted_step_by_slot": {slot: 0.25 for slot in SLOTS},
        "common_550nm_point_center_roi": {
            "status": "point_center_geometry_overlap_only", "nonempty": True,
            "row_angle_deg": [-extent, extent], "column_angle_deg": [-extent, extent],
        },
    }
    return cases, images, masks, comparison


def test_same_physical_edge_has_matched_object_angle_mtf_across_native_pitches():
    cases, images, masks, comparison = _angular_edge_fixture()
    result = _matched_angular_edge(cases, images, masks, comparison)
    assert result["available"] is True
    assert result["product_ranking_allowed"] is False
    assert result["angular_roi_area_matched"] is False
    assert result["angular_roi_contract"] == "shared_overlap_native_rectangles_different_extents"
    assert len(result["pairs"]) == 4
    estimates = [
        result["per_slot"][slot]["mtf50_cycles_per_degree_object_angle"]
        for slot in SLOTS
    ]
    assert max(estimates) - min(estimates) < 0.02 * np.mean(estimates)
    assert [result["per_slot"][slot]["native_roi_shape"][0] for slot in SLOTS] == [
        32, 28, 26, 24
    ]
    assert all(result["per_slot"][slot]["chromatic_center_displacement_native_steps"] < 0.1
               for slot in SLOTS)


def test_common_grid_edge_uses_identical_angular_area_and_sampling():
    cases, images, masks, comparison = _angular_edge_fixture()
    native = _matched_angular_edge(cases, images, masks, comparison)
    result = evaluate_common_grid_edge(cases, images, masks, comparison, native)
    assert result["available"] is True
    assert result["angular_roi_area_matched"] is True
    assert result["angular_sample_grid_matched"] is True
    assert result["product_ranking_allowed"] is False
    footprint = result["model_550nm_point_cell_footprint"]
    assert footprint["available"] is True
    assert footprint["actual_pixel_integration_or_psf_footprint_compared"] is False
    assert footprint["max_to_min_median_cell_area_ratio"] > 1.0
    assert result["grid"]["shape_row_col"][0] == result["grid"]["shape_row_col"][1]
    assert 16 <= result["grid"]["shape_row_col"][0] <= 32
    assert result["grid"]["area_square_degrees"] > 0
    assert len(result["pairs"]) == 4
    estimates = [
        result["per_slot"][slot]["mtf50_cycles_per_degree_object_angle"]
        for slot in SLOTS
    ]
    assert max(estimates) - min(estimates) < 0.03 * np.mean(estimates)

    # The new diagnostic does not manufacture an edge or extrapolate through
    # a missing ISP region on one camera.
    masks["L1S1"][10:23, 10:23] = False
    blocked = evaluate_common_grid_edge(cases, images, masks, comparison, native)
    assert blocked["available"] is False
    assert blocked["angular_roi_area_matched"] is False
    assert "No central" in blocked["reasons"][0]


def test_historical_common_grid_omission_keeps_existing_numeric_metrics_checked():
    cases, images, masks, comparison = _angular_edge_fixture()
    native = _matched_angular_edge(cases, images, masks, comparison)
    current = evaluate_common_grid_edge(cases, images, masks, comparison, native)
    assert current["available"] is True
    historical = deepcopy(current)
    del historical["model_550nm_point_cell_footprint"]
    assert _saved_shared_metric_matches(historical, current) is True
    assert _saved_shared_metric_matches(current, current) is True

    historical["per_slot"]["L1S1"]["mtf50_cycles_per_degree_object_angle"] += 1.0
    assert _saved_shared_metric_matches(historical, current) is False
    assert _saved_shared_metric_matches(None, current) is False


def test_point_cell_proxy_exposes_anisotropic_equal_area_and_unequal_area():
    cases, _, _, _ = _angular_edge_fixture()
    dimensions = {
        "L1S1": (0.001, 0.002),
        "L1S2": (0.002, 0.001),  # Same area, different axis widths.
        "L2S1": (0.001, 0.001),
        "L2S2": (0.002, 0.002),
    }
    yy, xx = np.indices((32, 32), dtype=float)
    for slot, (row_width, col_width) in dimensions.items():
        centers = np.stack(
            ((yy - 15.5) * row_width, (xx - 15.5) * col_width), axis=-1
        )
        cases[slot]["conditions"]["native_output_angular_grid"][
            "distortion_aware_point_center"
        ]["coordinates_deg_row_col_by_wavelength"] = np.stack(
            (centers, centers, centers)
        ).tolist()
    native = {
        "available": True,
        "per_slot": {
            slot: {"native_roi_row_col_exclusive": [[4, 28], [4, 28]]}
            for slot in SLOTS
        },
    }
    result = _model_point_cell_footprint(cases, native)
    assert result["available"] is True
    assert result["per_slot"]["L1S1"]["median_cell_area_square_degrees"] == pytest.approx(
        2e-6
    )
    assert result["per_slot"]["L1S2"]["median_cell_area_square_degrees"] == pytest.approx(
        2e-6
    )
    assert result["per_slot"]["L1S1"]["median_row_side_deg"] == pytest.approx(0.001)
    assert result["per_slot"]["L1S2"]["median_row_side_deg"] == pytest.approx(0.002)
    assert result["max_to_min_median_cell_area_ratio"] == pytest.approx(4.0)
    assert result["max_to_min_median_row_side_ratio"] == pytest.approx(2.0)
    assert result["max_to_min_median_col_side_ratio"] == pytest.approx(2.0)
    assert result["proxy_within_one_percent_all_axes_and_area"] is False
    assert result["actual_pixel_integration_or_psf_footprint_compared"] is False


def test_common_grid_refuses_missing_or_invalid_point_cell_map():
    cases, images, masks, comparison = _angular_edge_fixture()
    native = _matched_angular_edge(cases, images, masks, comparison)
    assert native["available"] is True
    del cases["L1S1"]["conditions"]["native_output_angular_grid"][
        "distortion_aware_point_center"
    ]
    result = evaluate_common_grid_edge(cases, images, masks, comparison, native)
    assert result["available"] is False
    assert result["model_550nm_point_cell_footprint"]["available"] is False
    assert "lacks a usable 550 nm" in result["reasons"][0]

    cases, images, masks, comparison = _angular_edge_fixture()
    native = _matched_angular_edge(cases, images, masks, comparison)
    map_values = cases["L2S2"]["conditions"]["native_output_angular_grid"][
        "distortion_aware_point_center"
    ]["coordinates_deg_row_col_by_wavelength"]
    map_values[1][16][16][0] = float("nan")
    result = evaluate_common_grid_edge(cases, images, masks, comparison, native)
    assert result["available"] is False
    assert "nonfinite point-center" in result["reasons"][0]


def test_common_grid_can_use_coarsest_vetted_native_centers_without_upsampling():
    steps = dict(zip(
        SLOTS, (0.0010408186, 0.0011943820, 0.0016707399, 0.0019172426),
        strict=True,
    ))
    cases, images, masks, comparison = _angular_edge_fixture(steps)
    native = _matched_angular_edge(cases, images, masks, comparison)
    assert native["available"] is True
    assert native["per_slot"]["L2S2"]["native_roi_shape"] == [16, 16]
    result = evaluate_common_grid_edge(cases, images, masks, comparison, native)
    assert result["available"] is True
    assert result["grid"]["grid_geometry"] == "vetted_native_reference_point_centers"
    assert result["grid"]["reference_slot"] == "L2S2"
    assert result["grid"]["shape_row_col"] == [16, 16]
    assert result["angular_roi_area_matched"] is True
    assert result["pixel_integrated_effective_area_matched"] is False
    assert result["product_ranking_allowed"] is False


def test_quality_v2_default_preserves_explicit_v1_contract():
    selected = default_spec()
    assert selected["schema"] == SCHEMA_V2
    selected["acknowledge_simulation_limits"] = True
    selected["source_jobs"] = {slot: slot for slot in SLOTS}
    assert specification(selected)["schema"] == SCHEMA_V2
    selected["schema"] = SCHEMA
    assert specification(selected)["schema"] == SCHEMA


def test_common_angle_edge_refuses_coarse_scene_chromatic_shift_or_small_overlap():
    cases, images, masks, comparison = _angular_edge_fixture()
    comparison["source_step_over_min_distorted_step_by_slot"]["L1S1"] = 0.9
    result = _matched_angular_edge(cases, images, masks, comparison)
    assert result["available"] is False
    assert "twice as dense" in result["reasons"][0]

    cases, images, masks, comparison = _angular_edge_fixture()
    point = cases["L1S1"]["conditions"]["native_output_angular_grid"][
        "distortion_aware_point_center"
    ]
    coords = np.asarray(point["coordinates_deg_row_col_by_wavelength"])
    coords[0] *= 1.02
    point["coordinates_deg_row_col_by_wavelength"] = coords.tolist()
    result = _matched_angular_edge(cases, images, masks, comparison)
    assert result["available"] is False
    assert "chromatic center displacement" in result["per_slot"]["L1S1"]["reason"]

    cases, images, masks, comparison = _angular_edge_fixture()
    comparison["common_550nm_point_center_roi"]["row_angle_deg"] = [-0.004, 0.004]
    comparison["common_550nm_point_center_roi"]["column_angle_deg"] = [-0.004, 0.004]
    result = _matched_angular_edge(cases, images, masks, comparison)
    assert result["available"] is False
    assert "fewer than 16" in result["per_slot"]["L1S1"]["reason"]


def test_condition_mismatch_and_wrong_cross_refuse_numeric_comparison(tmp_path, monkeypatch):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    spec = _matrix(service, monkeypatch, fovs=[0.04] * 4,
                   exposures=[0.004, 0.004, 0.004, 0.005])
    report, _ = evaluate_matrix(_inputs(service, spec), profile=spec["profile"])
    assert "exposure_s" in report["comparison"]["mismatched_control_fields"]
    assert report["comparison"]["edge_mtf50_diagnostic_delta_allowed"] is False
    malformed = deepcopy(spec)
    malformed["source_jobs"]["L2S2"] = spec["source_jobs"]["L1S2"]
    with pytest.raises(ValueError, match="distinct"):
        specification(malformed)
    malformed["source_jobs"]["L2S2"] = _source_job(
        service, slot="L1S2", fov=0.04, name_suffix=" duplicate"
    )
    with pytest.raises(ValueError, match="two distinct Lens revisions"):
        prepare(service, malformed)


def test_quality_job_round_trip_and_frozen_source_rejection(tmp_path, monkeypatch):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    spec = _matrix(service, monkeypatch)
    queued = submit(service, spec)
    assert service.registry.claim()["id"] == queued["id"]
    forged = deepcopy(service.registry.get_run(queued["id"]))
    forged["source_entries"][0]["source_binding"]["source_frame_seed"] = 99
    with pytest.raises(IntegrityError, match="Frozen Lens x Sensor sources"):
        execute(service, forged)
    result = execute(service, service.registry.get_run(queued["id"]))
    service.registry.finish(queued["id"], result=result)
    finished = get(service, queued["id"])
    assert finished["status"] == "succeeded"
    assert len(finished["artifacts"]) == 2
    summary = finished["result"]["summary"]
    assert summary["comparison"]["product_ranking_allowed"] is False
    assert summary["comparison"]["matched_horizontal_fov"] is False

    legacy = deepcopy(spec)
    legacy["schema"] = SCHEMA
    queued_v1 = submit(service, legacy)
    assert service.registry.claim()["id"] == queued_v1["id"]
    service.registry.finish(
        queued_v1["id"], result=execute(service, service.registry.get_run(queued_v1["id"]))
    )
    readback_v1 = get(service, queued_v1["id"])
    assert readback_v1["result"]["summary"]["schema"] == SCHEMA
    assert "common_grid_object_angle_edge_mtf50" not in (
        readback_v1["result"]["summary"]["comparison"]
    )


def test_dev28_sealed_wording_reads_back_but_numeric_change_is_rejected(
    tmp_path, monkeypatch,
):
    dev28_wording = {
        "reasons": [
            "Distortion-aware point-center maps describe geometry only; a common "
            "PSF/pixel-integrated output grid, angular ROI resampling and comparable "
            "frequency metric are not established."
        ],
        "scope": "individual source diagnostics only",
        "pairwise_diagnostic_deltas": [{
            "edge_mtf50_delta_available": False,
            "reason": "Full 2 x 2 scene and metric comparability gate did not pass.",
        }],
        "common_grid_object_angle_edge_mtf50": {
            "available": True, "angular_roi_area_matched": True,
            "pixel_integrated_effective_area_matched": False,
            "product_ranking_allowed": False,
        },
    }
    normalized = _normalize_dev28_comparison_wording(dev28_wording)
    assert "angular ROI resampling" not in normalized["reasons"][0]
    assert "PSF/pixel-integrated native RAW output footprints" in normalized["reasons"][0]
    assert normalized["common_grid_object_angle_edge_mtf50"] == (
        dev28_wording["common_grid_object_angle_edge_mtf50"]
    )
    assert normalized["scope"] != dev28_wording["scope"]
    assert normalized["pairwise_diagnostic_deltas"][0]["edge_mtf50_delta_available"] is False

    service = PlatformService(tmp_path / "state", WORKSPACE)
    spec = _matrix(service, monkeypatch)
    queued = submit(service, spec)
    assert service.registry.claim()["id"] == queued["id"]
    service.registry.finish(
        queued["id"], result=execute(service, service.registry.get_run(queued["id"]))
    )
    sealed = deepcopy(get(service, queued["id"]))
    historical = deepcopy(sealed)
    comparison = historical["result"]["summary"]["comparison"]
    comparison["scope"] = "individual source diagnostics only"
    for pair in comparison["pairwise_diagnostic_deltas"]:
        pair["reason"] = "Full 2 x 2 scene and metric comparability gate did not pass."
    del comparison["common_grid_object_angle_edge_mtf50"][
        "model_550nm_point_cell_footprint"
    ]

    def seal(payload):
        report = service.registry.store_blob(
            canonical(payload["result"]["summary"]).encode(),
            "lens-sensor-quality-study-result.json", "application/json",
        )
        payload["artifacts"] = [
            item for item in payload["artifacts"]
            if item["name"] != "lens-sensor-quality-study-result.json"
        ] + [report]
        payload["result"]["artifacts"] = payload["artifacts"]

    seal(historical)
    original_get_run = service.registry.get_run
    monkeypatch.setattr(
        service.registry, "get_run",
        lambda identity: historical if identity == queued["id"] else original_get_run(identity),
    )
    assert get(service, queued["id"])["result"]["summary"]["comparison"][
        "common_grid_object_angle_edge_mtf50"
    ]["product_ranking_allowed"] is False

    tampered = deepcopy(historical)
    tampered["result"]["summary"]["comparison"][
        "source_step_over_min_distorted_step_by_slot"
    ]["L1S1"] = 0.99
    seal(tampered)
    monkeypatch.setattr(
        service.registry, "get_run",
        lambda identity: tampered if identity == queued["id"] else original_get_run(identity),
    )
    with pytest.raises(IntegrityError, match="Saved quality study differs"):
        get(service, queued["id"])


def test_quality_api_sdk_reuses_four_saved_jobs(tmp_path, monkeypatch):
    state = tmp_path / "state"
    service = PlatformService(state, WORKSPACE)
    spec = _matrix(service, monkeypatch)
    app = create_app(state, WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        client = object.__new__(PlatformClient)
        client.http = transport
        template = client.lens_sensor_quality_template()
        assert len(template["source_jobs"]) == 4
        assert {item["id"] for item in template["source_jobs"]} == set(spec["source_jobs"].values())
        queued = client.submit_lens_sensor_quality(spec)
        assert transport.get(f"/api/v1/jobs/{queued['id']}").json()["detail_path"] == (
            "lens-sensor-quality-studies/" + queued["id"]
        )
        assert service.registry.claim()["id"] == queued["id"]
        service.registry.finish(
            queued["id"], result=execute(service, service.registry.get_run(queued["id"]))
        )
        assert client.lens_sensor_quality(queued["id"])["status"] == "succeeded"
        assert any(item["id"] == queued["id"] for item in client.list_lens_sensor_quality())
        exported = transport.get(f"/api/v1/lens-sensor-quality-studies/{queued['id']}/export")
        assert exported.status_code == 200
        assert (
            exported.json()["result"]["summary"]["comparison"]["product_ranking_allowed"]
            is False
        )
