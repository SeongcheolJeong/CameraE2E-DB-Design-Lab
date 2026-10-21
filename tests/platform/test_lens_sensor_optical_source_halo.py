"""Full-kernel optical ROI source-boundary checks without image execution."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from camera_platform.lens_catalog_bridge import _asset, _revision_source
from camera_platform.lens_sensor_optical_source_halo import (
    _chunked_geometry_query_bounds,
    preflight_strict_optical_source_halo,
)
from camera_platform.lens_sensor_tile_plan import _geometry_query_bounds
from pyisetcam.lens_patents import lens_patent_raytrace_optics

WORKSPACE = Path(__file__).resolve().parents[2]
P0014_REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"


def _strict_optics() -> dict:
    fields = np.array([0.0, 0.1])
    waves = np.array([500.0, 600.0])

    def table(values: np.ndarray) -> dict:
        return {
            "field_height_mm": fields.copy(),
            "wavelength_nm": waves.copy(),
            "function": values,
            "field_coordinate": "ideal_image_height",
        }

    psf = np.zeros((5, 5, 2, 2))
    psf[2, 2, :, :] = 1.0
    psf_table = table(psf)
    psf_table.update({
        "sample_spacing_mm": np.array([0.001, 0.001]),
        "coordinate_convention": "pixel_centers",
        "row_axis": "y_up",
        "psf_reference_axis": "+x",
    })
    return {
        "model": "raytrace",
        "focal_length_m": 0.01,
        "f_number": 2.0,
        "raytrace": {
            "contract_version": 2,
            "physics_policy": "strict",
            "coordinate_convention": "pixel_centers",
            "object_distance_m": np.inf,
            "effective_focal_length_m": 0.01,
            "effective_f_number": 2.0,
            "geometry": table(np.array([[0.0, 0.0], [0.1, 0.1]])),
            "relative_illumination": table(np.ones((2, 2))),
            "psf": psf_table,
        },
    }


def test_offcenter_roi_reports_full_psf_and_inverse_geometry_source_bounds() -> None:
    result = preflight_strict_optical_source_halo(
        _strict_optics(),
        global_scene_shape_row_col=(64, 64),
        optical_roi_row_col_exclusive=(20, 24, 29, 33),
        oi_pitch_um=1.0,
        wavelengths_nm=(500.0, 600.0),
    )
    assert result["status"] == "inside_declared_scene"
    assert result["sampled_psf_kernel_shape_row_col"] == [5, 5]
    assert result["sampled_psf_envelope_row_col_exclusive_unclipped"] == [18, 26, 27, 35]
    assert result["pregeometry_oi_row_col_exclusive"] == [18, 26, 27, 35]
    assert result["source_query_union_row_col_exclusive_unclipped"] == [17, 27, 26, 36]
    assert result["geometry_query_cells"] == 8 * 8 * 2
    assert all(
        not item["source_query_touches_global_scene_boundary"]
        for item in result["source_query_by_wavelength"]
    )
    assert result["declared_source_support_fits"] is True
    assert result["physical_psf_qualified"] is False
    assert result["physical_scene_halo_qualified"] is False
    assert result["product_fov_qualified"] is False
    assert result["optical_or_sensor_calculation_executed"] is False


def test_finite_global_zero_boundary_is_reported_without_scene_execution() -> None:
    result = preflight_strict_optical_source_halo(
        _strict_optics(),
        global_scene_shape_row_col=(64, 64),
        optical_roi_row_col_exclusive=(0, 4, 60, 64),
        oi_pitch_um=1.0,
        wavelengths_nm=(500.0, 600.0),
    )
    assert result["status"] == "global_constant_zero_boundary_touched"
    assert result["sampled_psf_envelope_row_col_exclusive_unclipped"] == [-2, 6, 58, 66]
    assert result["pregeometry_oi_row_col_exclusive"] == [0, 6, 58, 64]
    assert result["sampled_psf_envelope_touches_global_scene_boundary"] is True
    assert result["source_query_touches_global_scene_boundary"] is True
    assert result["source_query_union_row_col_exclusive_clipped"] == [0, 7, 57, 64]
    assert result["declared_source_support_fits"] is False


def test_psf_and_geometry_query_policies_block_before_large_query() -> None:
    request = {
        "global_scene_shape_row_col": (64, 64),
        "optical_roi_row_col_exclusive": (20, 24, 29, 33),
        "wavelengths_nm": (500.0, 600.0),
    }
    optics = _strict_optics()
    psf_blocked = preflight_strict_optical_source_halo(
        optics, **request, oi_pitch_um=0.0001
    )
    assert psf_blocked["status"] == "blocked_psf_grid_policy"
    assert "sampled_psf_kernel_shape_row_col" not in psf_blocked

    geometry_blocked = preflight_strict_optical_source_halo(
        optics, **request, oi_pitch_um=1.0, max_geometry_query_cells=1
    )
    assert geometry_blocked["status"] == "blocked_geometry_query_policy"
    assert geometry_blocked["geometry_query_cells"] == 128
    assert "source_query_by_wavelength" not in geometry_blocked

    raised_budget = preflight_strict_optical_source_halo(
        optics, **request, oi_pitch_um=1.0, max_geometry_query_cells=8_000_001
    )
    assert raised_budget["status"] == "inside_declared_scene"
    assert raised_budget["max_geometry_query_cells"] == 8_000_001
    with pytest.raises(ValueError, match="Geometry query chunk policy"):
        preflight_strict_optical_source_halo(
            optics, **request, oi_pitch_um=1.0, max_geometry_query_chunk_cells=65_537
        )


@pytest.mark.parametrize("rect", [(0, 29, 0, 31), (4, 16, 5, 28), (1, 2, 2, 3)])
@pytest.mark.parametrize("chunk_cells", [1, 7, 64, 1_024])
def test_chunked_geometry_query_bounds_match_monolithic_exactly(rect, chunk_cells) -> None:
    shape = (29, 31)
    ideal = np.array([0.0, 0.01, 0.03, 0.1])
    distorted = np.array([0.0, 0.012, 0.035, 0.11])
    expected = _geometry_query_bounds(rect, shape, 0.001, ideal, distorted)
    actual, count, largest = _chunked_geometry_query_bounds(
        rect, shape, 0.001, ideal, distorted, max_chunk_cells=chunk_cells
    )
    assert actual == expected
    assert count >= 1
    assert largest <= chunk_cells


def test_above_default_total_budget_uses_bounded_chunks_without_full_scan(monkeypatch) -> None:
    from camera_platform import lens_sensor_optical_source_halo as halo

    scanned: list[tuple[int, int, int, int]] = []

    def checked_bounds(rect, shape, pitch_mm, ideal_mm, distorted_mm):
        scanned.append(rect)
        assert (rect[1] - rect[0]) * (rect[3] - rect[2]) <= 4_096
        return rect[0] - 1, rect[1] + 1, rect[2] - 1, rect[3] + 1

    monkeypatch.setattr(halo, "_geometry_query_bounds", checked_bounds)
    # One wavelength and a 2048 x 4096 pregeometry raster: more than the old
    # 8M total cap while each inverse-geometry call scans at most 4096 cells.
    result = preflight_strict_optical_source_halo(
        _strict_optics(),
        global_scene_shape_row_col=(2_050, 4_098),
        optical_roi_row_col_exclusive=(1, 2_049, 1, 4_097),
        oi_pitch_um=1.0,
        wavelengths_nm=(500.0,),
        max_geometry_query_cells=9_000_000,
        max_geometry_query_chunk_cells=4_096,
    )
    assert result["geometry_query_cells"] > 8_000_000
    assert result["status"] == "global_constant_zero_boundary_touched"
    assert result["geometry_query_chunk_count"] == len(scanned)
    assert result["geometry_query_chunk_count"] > 1
    assert result["largest_geometry_query_chunk_cells"] <= 4_096
    assert result["max_geometry_query_cells"] == 9_000_000
    assert result["geometry_query_work_budget_is_time_or_hardware_limit"] is False


def test_requested_wavelength_requires_every_strict_lut_to_cover_it() -> None:
    optics = _strict_optics()
    optics["raytrace"]["geometry"]["wavelength_nm"] = np.array([500.0, 650.0])
    with pytest.raises(ValueError, match="strict psf coverage"):
        preflight_strict_optical_source_halo(
            optics,
            global_scene_shape_row_col=(64, 64),
            optical_roi_row_col_exclusive=(20, 24, 29, 33),
            oi_pitch_um=1.0,
            wavelengths_nm=(500.0, 650.0),
        )


@pytest.mark.parametrize("name", ["psf", "relative_illumination"])
def test_optical_field_table_must_fit_geometry_coverage(name: str) -> None:
    optics = _strict_optics()
    optics["raytrace"][name]["field_height_mm"] = np.array([0.0, 0.2])
    with pytest.raises(ValueError, match="table field coverage exceeds"):
        preflight_strict_optical_source_halo(
            optics,
            global_scene_shape_row_col=(64, 64),
            optical_roi_row_col_exclusive=(20, 24, 29, 33),
            oi_pitch_um=1.0,
            wavelengths_nm=(500.0, 600.0),
        )


def test_p0014_2048_same_pitch_center_and_offcenter_boundary_diagnostic() -> None:
    root = WORKSPACE / "camerae2e_db"
    if not (root / "catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB unavailable")
    revision, snapshot, lens_db, _ = _revision_source(P0014_REVISION, root)
    simulation_id, psf_path, check, _ = _asset(revision, snapshot, lens_db, root)
    optics = lens_patent_raytrace_optics(
        simulation_id,
        db_path=lens_db,
        psf_dir=psf_path.parent,
        allow_approximate_radiometry=True,
    )
    assert optics["raytrace"]["asset_sha256"] == check["asset_sha256"]
    def planned(center_col: int) -> dict:
        return preflight_strict_optical_source_halo(
            optics,
            global_scene_shape_row_col=(2048, 2048),
            optical_roi_row_col_exclusive=(1006, 1042, center_col - 18, center_col + 18),
            oi_pitch_um=0.575606513,
            wavelengths_nm=(450.0, 550.0, 650.0),
        )

    center = planned(1024)
    offcenter = planned(1536)
    boundary = planned(1792)
    assert center["status"] == "inside_declared_scene"
    assert offcenter["status"] == "inside_declared_scene"
    assert offcenter["sampled_psf_kernel_shape_row_col"] == [547, 547]
    assert offcenter["sampled_psf_envelope_row_col_exclusive_unclipped"] == [
        733, 1315, 1245, 1827
    ]
    assert offcenter["source_query_union_row_col_exclusive_unclipped"][3] < 2048
    assert boundary["status"] == "global_constant_zero_boundary_touched"
    assert boundary["sampled_psf_envelope_touches_global_scene_boundary"] is True
    assert boundary["source_query_union_row_col_exclusive_unclipped"][3] > 2048
    assert boundary["declared_source_support_fits"] is False
    assert all(
        record["physical_scene_halo_qualified"] is False
        and record["product_fov_qualified"] is False
        and record["full_frame_execution_qualified"] is False
        for record in (center, offcenter, boundary)
    )
