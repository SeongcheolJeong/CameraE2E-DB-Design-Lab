"""Global analytic Scene -> strict pre-PSF OI tile parity."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from camera_platform.lens_catalog_bridge import _asset, _revision_source
from camera_platform.lens_sensor_scene_geometry_tile import (
    d65_analytic_bright_photons,
    make_analytic_strict_geometry_provider,
)
from camera_platform.offline_assets import offline_asset_store
from pyisetcam.lens_patents import lens_patent_raytrace_optics
from pyisetcam.optics import (
    _cos4th_factor,
    _linspace_window,
    _radiance_to_irradiance,
    _strict_raytrace_apply_psf_bounded_roi_provider,
    _strict_raytrace_geometry,
    oi_compute,
    oi_create,
)
from pyisetcam.scene import scene_adjust_luminance, scene_interpolate_w, scene_set
from pyisetcam.sensor_comparison import _make_scene
from pyisetcam.types import Scene

WORKSPACE = Path(__file__).resolve().parents[2]
WAVES = np.array([500.0, 600.0])


def _optics(*, offaxis_method: str = "skip") -> dict:
    fields = np.array([0.0, 0.1])

    def table(values: np.ndarray) -> dict:
        return {
            "field_height_mm": fields.copy(),
            "wavelength_nm": WAVES.copy(),
            "function": values,
            "field_coordinate": "ideal_image_height",
        }

    psf = np.zeros((3, 3, 2, 2), dtype=float)
    psf[1, 1, :, :] = 1.0
    psf_table = table(psf)
    psf_table.update(
        sample_spacing_mm=np.array([0.001, 0.001]),
        coordinate_convention="pixel_centers",
        row_axis="y_up",
        psf_reference_axis="+x",
    )
    return {
        "model": "raytrace",
        "focal_length_m": 0.01,
        "f_number": 2.0,
        "offaxis_method": offaxis_method,
        "transmittance": {"wave": WAVES.copy(), "scale": np.array([0.7, 0.9])},
        "raytrace": {
            "contract_version": 2,
            "physics_policy": "strict",
            "coordinate_convention": "pixel_centers",
            "object_distance_m": math.inf,
            "effective_focal_length_m": 0.01,
            "effective_f_number": 2.0,
            "max_fov_deg": 1.0,
            "geometry": table(np.array([[0.0, 0.0], [0.08, 0.09]])),
            "relative_illumination": table(np.array([[1.0, 1.0], [0.65, 0.75]])),
            "psf": psf_table,
        },
    }


def _reference_scene(
    shape: tuple[int, int], fov_deg: float, bright: np.ndarray, offset_deg: float
) -> Scene:
    rows, cols = shape
    row, col = np.indices(shape, dtype=float)
    step = 2 * math.tan(math.radians(fov_deg) / 2) / cols
    offset = math.tan(math.radians(offset_deg))
    edge = ((col + 0.5 - cols / 2) - (row + 0.5 - rows / 2) / 12) * step
    mask = np.where(edge > offset, 1.0, 0.02)
    vfov = math.degrees(2 * math.atan(math.tan(math.radians(fov_deg) / 2) * rows / cols))
    return Scene(
        fields={
            "wave": WAVES.copy(),
            "fov_deg": fov_deg,
            "vfov_deg": vfov,
            "distance_m": math.inf,
            "rows": rows,
            "cols": cols,
        },
        data={
            "photons": np.broadcast_to(bright, (rows, cols, bright.size)).copy()
            * mask[..., None]
        },
    )


@pytest.mark.parametrize("shape", [(40, 64), (41, 65)])
@pytest.mark.parametrize("offaxis", ["skip", "cos4th"])
def test_global_scene_and_geometry_tiles_match_full_strict_path(
    shape: tuple[int, int], offaxis: str
) -> None:
    rows, cols = shape
    fov = math.degrees(2 * math.atan(cols * 0.001 / 20))
    bright = np.array([123.0, 456.0])
    offset_deg = -0.0007
    optics = _optics(offaxis_method=offaxis)
    provider = make_analytic_strict_geometry_provider(
        optics,
        scene_shape_row_col=shape,
        scene_horizontal_fov_deg=fov,
        wavelengths_nm=WAVES,
        bright_side_photons=bright,
        analytic_slanted_edge_offset_deg=offset_deg,
    )
    full = _reference_scene(shape, fov, bright, offset_deg)
    pregeometry = _radiance_to_irradiance(full.data["photons"], optics, full)
    if offaxis == "cos4th":
        pregeometry = pregeometry * _cos4th_factor(rows, cols, optics, full)[:, :, None]
    reference = _strict_raytrace_geometry(pregeometry, WAVES, optics, full)

    for rect in [(0, 8, 0, 13), (12, 31, 18, 49), (rows - 10, rows, cols - 12, cols)]:
        r0, r1, c0, c1 = rect
        np.testing.assert_array_equal(
            provider.read_scene_tile(*rect), full.data["photons"][r0:r1, c0:c1]
        )
        np.testing.assert_allclose(
            provider.read_tile(*rect), reference[r0:r1, c0:c1], rtol=5e-13, atol=5e-13
        )
    assert provider.metadata["full_scene_materialized"] is False
    assert provider.metadata["source_identity"] == provider.source_identity
    assert provider.metadata["pregeometry_offaxis_method"] == offaxis
    assert provider.global_shape == shape
    assert provider.oi_pitch_um == pytest.approx(1.0)


def test_global_large_grid_accepts_small_center_tile_under_guard() -> None:
    optics = _optics()
    optics["raytrace"]["max_fov_deg"] = 2.0
    shape = (4096, 4096)
    fov = 1.0
    provider = make_analytic_strict_geometry_provider(
        optics,
        scene_shape_row_col=shape,
        scene_horizontal_fov_deg=fov,
        wavelengths_nm=WAVES,
        bright_side_photons=[123.0, 456.0],
        analytic_slanted_edge_offset_deg=0.0,
        max_tile_cells=1024,
    )
    assert provider.read_tile(2044, 2052, 2044, 2052).shape == (8, 8, 2)
    with pytest.raises(ValueError, match="tile-cell guard"):
        provider.read_tile(2000, 2100, 2000, 2100)


def test_cell_area_edge_integrates_a_center_crossing_without_changing_default() -> None:
    parameters = {
        "scene_shape_row_col": (7, 11),
        "scene_horizontal_fov_deg": 0.5,
        "wavelengths_nm": WAVES,
        "bright_side_photons": [100.0, 200.0],
        "analytic_slanted_edge_offset_deg": 0.0,
    }
    point = make_analytic_strict_geometry_provider(_optics(), **parameters)
    explicit_point = make_analytic_strict_geometry_provider(
        _optics(), **parameters, source_rasterization="point_sampled"
    )
    area = make_analytic_strict_geometry_provider(
        _optics(), **parameters, source_rasterization="cell_area_integrated"
    )
    # The infinite straight edge bisects the middle cell exactly. Historical
    # point sampling assigns the boundary to the dark side; area integration
    # gives half of each region's radiance instead of choosing one center.
    assert point.read_scene_tile(3, 4, 5, 6)[0, 0, 0] == pytest.approx(2.0)
    assert area.read_scene_tile(3, 4, 5, 6)[0, 0, 0] == pytest.approx(51.0)
    assert point.source_identity == explicit_point.source_identity
    assert area.source_identity != point.source_identity
    assert area.metadata["source_rasterization"] == "cell_area_integrated"
    assert "area_average" in area.metadata["source_cell_interpretation"]
    assert point.metadata["source_rasterization"] == "point_sampled"


def test_cell_area_edge_is_exact_under_fixed_fov_two_by_two_refinement() -> None:
    common = {
        "scene_horizontal_fov_deg": 0.5,
        "wavelengths_nm": WAVES,
        "bright_side_photons": [100.0, 200.0],
        "analytic_slanted_edge_offset_deg": 0.003,
        "source_rasterization": "cell_area_integrated",
    }
    coarse = make_analytic_strict_geometry_provider(
        _optics(), scene_shape_row_col=(7, 11), **common
    )
    refined = make_analytic_strict_geometry_provider(
        _optics(), scene_shape_row_col=(14, 22), **common
    )
    coarse_scene = coarse.read_scene_tile(0, 7, 0, 11)
    refined_scene = refined.read_scene_tile(0, 14, 0, 22)
    reaggregated = refined_scene.reshape(7, 2, 11, 2, 2).mean(axis=(1, 3))
    np.testing.assert_allclose(reaggregated, coarse_scene, rtol=0, atol=1e-12)
    # On-demand tile boundaries do not recenter the analytic edge.
    np.testing.assert_array_equal(
        refined.read_scene_tile(4, 9, 6, 15), refined_scene[4:9, 6:15]
    )
    assert coarse.metadata["horizontal_fov_deg"] == refined.metadata["horizontal_fov_deg"]
    assert coarse.metadata["vertical_fov_deg"] == pytest.approx(
        refined.metadata["vertical_fov_deg"]
    )


@pytest.mark.parametrize("mode", ["interpolated", "", 2, None])
def test_analytic_source_rejects_unsupported_rasterization(mode: object) -> None:
    with pytest.raises(ValueError, match="source_rasterization"):
        make_analytic_strict_geometry_provider(
            _optics(),
            scene_shape_row_col=(7, 11),
            scene_horizontal_fov_deg=0.5,
            wavelengths_nm=WAVES,
            bright_side_photons=[100.0, 200.0],
            analytic_slanted_edge_offset_deg=0.0,
            source_rasterization=mode,
        )


@pytest.mark.parametrize("count", [1, 2, 3, 40, 41, 64, 65])
def test_windowed_global_axes_match_full_linspace_at_edges_and_center(count: int) -> None:
    start = -0.173456789
    stop = 0.173456789
    full = np.linspace(start, stop, count)
    windows = (
        (0, min(3, count)),
        (max(0, count // 2 - 2), min(count, count // 2 + 2)),
        (max(0, count - 3), count),
    )
    for first, last in windows:
        np.testing.assert_array_equal(
            _linspace_window(start, stop, count, first, last), full[first:last]
        )
        np.testing.assert_array_equal(
            _linspace_window(start, stop, count, first, last, reverse=True),
            full[::-1][first:last],
        )


def test_very_large_virtual_geometry_tile_never_allocates_full_axes(monkeypatch) -> None:
    # The declared shape is coordinates only. Building either full linspace
    # would allocate 80 MB, even though the requested geometry tile is 4x4.
    size = 10_000_000
    provider = make_analytic_strict_geometry_provider(
        _optics(offaxis_method="cos4th"),
        scene_shape_row_col=(size, size),
        scene_horizontal_fov_deg=0.5,
        wavelengths_nm=WAVES,
        bright_side_photons=[123.0, 456.0],
        analytic_slanted_edge_offset_deg=0.0,
        max_tile_cells=64,
    )
    original_linspace = np.linspace

    def bounded_linspace(*args, **kwargs):
        count = args[2] if len(args) >= 3 else kwargs.get("num", 50)
        if count > 1024:
            raise AssertionError("A full virtual coordinate axis was allocated.")
        return original_linspace(*args, **kwargs)

    monkeypatch.setattr(np, "linspace", bounded_linspace)
    center = size // 2
    tile = provider.read_tile(center - 2, center + 2, center - 2, center + 2)
    assert tile.shape == (4, 4, 2)
    assert np.isfinite(tile).all()


def test_very_large_virtual_psf_roi_never_allocates_full_axes(monkeypatch) -> None:
    size = 10_000_000
    center = size // 2
    original_linspace = np.linspace

    def bounded_linspace(*args, **kwargs):
        count = args[2] if len(args) >= 3 else kwargs.get("num", 50)
        if count > 1024:
            raise AssertionError("A full virtual coordinate axis was allocated.")
        return original_linspace(*args, **kwargs)

    monkeypatch.setattr(np, "linspace", bounded_linspace)
    optical, evidence = _strict_raytrace_apply_psf_bounded_roi_provider(
        (size, size),
        lambda r0, r1, c0, c1: np.ones((r1 - r0, c1 - c0, WAVES.size)),
        _optics(),
        WAVES,
        1e-6,
        (2, 2),
        np.arange(0.0, 361.0, 90.0),
        output_roi_row_col=(center - 3, center + 3, center - 3, center + 3),
        source_identity="large-virtual-grid-test",
        source_tile_shape_row_col=(8, 8),
    )
    assert optical.shape == (6, 6, 2)
    assert np.isfinite(optical).all()
    assert evidence["source_tiles_processed"] <= 4
    assert evidence["global_field_coordinates_preserved"] is True


def test_analytic_edge_can_be_positioned_at_declared_offaxis_field() -> None:
    optics = _optics()
    optics["raytrace"]["max_fov_deg"] = 20.0
    provider = make_analytic_strict_geometry_provider(
        optics,
        scene_shape_row_col=(80, 80),
        scene_horizontal_fov_deg=10.0,
        wavelengths_nm=WAVES,
        bright_side_photons=[123.0, 456.0],
        analytic_slanted_edge_offset_deg=4.0,
    )
    assert provider.read_scene_tile(40, 41, 40, 41)[0, 0, 0] == pytest.approx(123.0 * 0.02)
    assert provider.read_scene_tile(40, 41, 78, 79)[0, 0, 0] == pytest.approx(123.0)
    with pytest.raises(ValueError, match="horizontal half-FOV"):
        make_analytic_strict_geometry_provider(
            optics,
            scene_shape_row_col=(80, 80),
            scene_horizontal_fov_deg=10.0,
            wavelengths_nm=WAVES,
            bright_side_photons=[123.0, 456.0],
            analytic_slanted_edge_offset_deg=5.1,
        )


def test_wide_rectangular_declared_grid_stays_virtual_and_lens_bounded() -> None:
    optics = _optics()
    optics["raytrace"]["max_fov_deg"] = 60.0
    provider = make_analytic_strict_geometry_provider(
        optics,
        scene_shape_row_col=(1024, 45_000),
        scene_horizontal_fov_deg=40.0,
        wavelengths_nm=WAVES,
        bright_side_photons=[123.0, 456.0],
        analytic_slanted_edge_offset_deg=15.0,
        max_tile_cells=16,
    )
    assert provider.read_scene_tile(512, 513, 35_000, 35_001)[0, 0, 0] == pytest.approx(
        123.0 * 0.02
    )
    assert provider.read_scene_tile(512, 513, 43_000, 43_001)[0, 0, 0] == pytest.approx(
        123.0
    )
    assert provider.metadata["full_scene_materialized"] is False
    optics["raytrace"]["max_fov_deg"] = 30.0
    with pytest.raises(ValueError, match="exceeds the loaded ray-trace analysis"):
        make_analytic_strict_geometry_provider(
            optics,
            scene_shape_row_col=(1024, 45_000),
            scene_horizontal_fov_deg=40.0,
            wavelengths_nm=WAVES,
            bright_side_photons=[123.0, 456.0],
            analytic_slanted_edge_offset_deg=15.0,
        )


def test_contract_rejects_grid_pitch_and_unsupported_offaxis() -> None:
    request = {
        "scene_shape_row_col": (40, 64),
        "scene_horizontal_fov_deg": math.degrees(2 * math.atan(64 * 0.001 / 20)),
        "wavelengths_nm": WAVES,
        "bright_side_photons": [123.0, 456.0],
        "analytic_slanted_edge_offset_deg": 0.0,
    }
    with pytest.raises(ValueError, match="Declared OI pitch differs"):
        make_analytic_strict_geometry_provider(_optics(), **request, declared_oi_pitch_um=2.0)
    with pytest.raises(ValueError, match="off-axis methods"):
        make_analytic_strict_geometry_provider(_optics(offaxis_method="unknown"), **request)
    provider = make_analytic_strict_geometry_provider(_optics(), **request)
    with pytest.raises(ValueError, match="global Scene/OI grid"):
        provider.read_tile(-1, 2, 0, 2)


def test_provider_copies_optics_and_detects_mutation_of_its_copied_state() -> None:
    original = _optics()
    provider = make_analytic_strict_geometry_provider(
        original,
        scene_shape_row_col=(40, 64),
        scene_horizontal_fov_deg=math.degrees(2 * math.atan(64 * 0.001 / 20)),
        wavelengths_nm=WAVES,
        bright_side_photons=[123.0, 456.0],
        analytic_slanted_edge_offset_deg=0.0,
    )
    baseline = provider.read_tile(12, 20, 24, 32)
    assert provider.optics is not original
    assert provider.metadata["full_optical_state_sha256"] == provider.optics_state_sha256
    original["raytrace"]["relative_illumination"]["function"][0, 0] = 0.1
    np.testing.assert_array_equal(provider.read_tile(12, 20, 24, 32), baseline)

    provider.optics["raytrace"]["relative_illumination"]["function"][0, 0] = 0.2
    with pytest.raises(ValueError, match="copied optics changed"):
        provider.read_tile(12, 20, 24, 32)


@pytest.mark.parametrize("fov", [0.2, 40.0])
def test_small_d65_bright_spectrum_matches_full_uniform_source(fov: float) -> None:
    if not (WORKSPACE / ".cache" / "upstream" / "isetcam").is_dir():
        pytest.skip("Pinned upstream assets unavailable")
    wavelength = (450.0, 550.0, 650.0)
    small = d65_analytic_bright_photons(
        workspace=WORKSPACE,
        horizontal_fov_deg=fov,
        luminance_cd_m2=1000.0,
        wavelengths_nm=wavelength,
    )
    assets = offline_asset_store(workspace=WORKSPACE)
    with assets.bound_default(assets):
        full, _ = _make_scene("uniform", (16, 16), fov, assets, source_size=(64, 64))
        scene_interpolate_w(full, wavelength, asset_store=assets)
        scene_adjust_luminance(full, 1000.0, asset_store=assets)
    np.testing.assert_allclose(small, full.data["photons"][0, 0], rtol=1e-13, atol=0)


def test_pinned_p0014_full_oi_compute_matches_analytic_geometry_tile_provider() -> None:
    root = WORKSPACE / "camerae2e_db"
    if not (root / "catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB unavailable")
    revision, snapshot, lens_db, _ = _revision_source(
        "6c48e967-74b4-5901-8186-ba6fafe4797b", root
    )
    simulation_id, psf_path, check, _ = _asset(revision, snapshot, lens_db, root)
    optics = lens_patent_raytrace_optics(
        simulation_id,
        db_path=lens_db,
        psf_dir=psf_path.parent,
        allow_approximate_radiometry=True,
    )
    assert optics["offaxis_method"] == "skip"
    fov = 0.05
    wave = np.array([450.0, 550.0, 650.0])
    assets = offline_asset_store(workspace=WORKSPACE)
    with assets.bound_default(assets):
        full_scene, _ = _make_scene(
            "uniform", (16, 16), fov, assets, source_size=(32, 32)
        )
        scene_interpolate_w(full_scene, wave, asset_store=assets)
        scene_adjust_luminance(full_scene, 1000.0, asset_store=assets)
        bright = d65_analytic_bright_photons(
            workspace=WORKSPACE,
            horizontal_fov_deg=fov,
            luminance_cd_m2=1000.0,
            wavelengths_nm=wave,
        )
        np.testing.assert_allclose(
            bright, full_scene.data["photons"][0, 0], rtol=1e-13, atol=0
        )
        row, col = np.indices((32, 32), dtype=float)
        step = 2 * math.tan(math.radians(fov) / 2) / 32
        edge = ((col + 0.5 - 16) - (row + 0.5 - 16) / 12) * step
        mask = np.where(edge > math.tan(math.radians(0.0)), 1.0, 0.02)
        scene_set(
            full_scene,
            "photons",
            np.asarray(full_scene.data["photons"]) * mask[..., None],
        )
        scene_set(full_scene, "distance", float(optics["raytrace"]["object_distance_m"]))
        oi = oi_create("pinhole", asset_store=assets)
        oi.fields["optics"] = optics
        oi.fields["psf_compute_mode"] = "bounded_streaming"
        oi.fields["psf_sample_angles_deg"] = np.arange(0.0, 361.0, 90.0)
        full_oi = oi_compute(oi, full_scene)

    provider = make_analytic_strict_geometry_provider(
        optics,
        scene_shape_row_col=(32, 32),
        scene_horizontal_fov_deg=fov,
        wavelengths_nm=wave,
        bright_side_photons=bright,
        analytic_slanted_edge_offset_deg=0.0,
        declared_oi_pitch_um=float(full_oi.fields["sample_spacing_m"]) * 1e6,
    )
    np.testing.assert_allclose(
        provider.read_scene_tile(3, 19, 7, 25),
        full_scene.data["photons"][3:19, 7:25],
        rtol=1e-13,
        atol=0,
    )
    pad_r, pad_c = full_oi.fields["padding_pixels"]
    roi, evidence = _strict_raytrace_apply_psf_bounded_roi_provider(
        provider.global_shape,
        provider.read_tile,
        optics,
        wave,
        float(full_oi.fields["sample_spacing_m"]),
        (pad_r, pad_c),
        np.arange(0.0, 361.0, 90.0),
        output_roi_row_col=(8, 24, 8, 24),
        source_identity=provider.source_identity,
        source_tile_shape_row_col=(16, 16),
    )
    np.testing.assert_allclose(
        roi,
        full_oi.data["photons"][pad_r + 8:pad_r + 24, pad_c + 8:pad_c + 24],
        rtol=2e-12,
        atol=2.0,
    )
    assert evidence["full_source_cube_allocated_by_operator"] is False
    assert evidence["caller_source_cube_residency"] == "unknown"
    assert evidence["global_field_coordinates_preserved"] is True
    assert check["asset_sha256"] == optics["raytrace"]["asset_sha256"]
