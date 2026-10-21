"""Physical invariants for the opt-in LUT contract, separate from MATLAB parity."""

from __future__ import annotations

from copy import deepcopy

import numpy as np
import pytest

from pyisetcam import optics as op
from pyisetcam.types import OpticalImage, Scene


def _optics() -> dict:
    fields = np.array([0.0, 0.6, 1.5])
    wave = np.array([500.0, 600.0])
    kernel = np.zeros((5, 5, 3, 2))
    kernel[2, 2, :, :] = 1.0

    def table(values: np.ndarray) -> dict:
        return {
            "field_height_mm": fields.copy(),
            "wavelength_nm": wave.copy(),
            "function": values,
            "field_coordinate": "ideal_image_height",
        }

    psf = table(kernel)
    psf.update(
        sample_spacing_mm=np.array([0.1, 0.1]),
        coordinate_convention="pixel_centers",
        row_axis="y_up",
        psf_reference_axis="+x",
    )
    return {
        "model": "raytrace",
        "name": "contract fixture",
        "focal_length_m": 0.01,
        "f_number": 2.0,
        "offaxis_method": "skip",
        "transmittance": {"wave": wave.copy(), "scale": np.ones(2)},
        "raytrace": {
            "contract_version": 2,
            "physics_policy": "strict",
            "coordinate_convention": "pixel_centers",
            "object_distance_m": np.inf,
            "effective_focal_length_m": 0.01,
            "effective_f_number": 2.0,
            "magnification": 0.0,
            "max_fov_deg": 180.0,
            "geometry": table(np.repeat(np.array([[0.0], [0.5], [1.25]]), 2, axis=1)),
            "relative_illumination": table(np.ones((3, 2))),
            "psf": psf,
        },
    }


def _scene(photons: np.ndarray | None = None, distance: float = np.inf) -> Scene:
    if photons is None:
        photons = np.ones((17, 17, 1))
    rows, cols = photons.shape[:2]
    fov = float(np.rad2deg(2 * np.arctan((cols * 0.0001 / 2) / 0.01)))
    return Scene(
        fields={
            "distance_m": distance,
            "fov_deg": fov,
            "vfov_deg": fov,
            "wave": np.array([550.0]),
            "rows": rows,
            "cols": cols,
        },
        data={"photons": photons},
    )


def _precompute(optics: dict, wave: tuple = (550.0,)) -> dict:
    return op._raytrace_precompute_psf(
        optics, np.array(wave), 17, 17, 0.0001, sample_angles_deg=np.arange(0.0, 361.0, 90.0)
    )


def test_sparse_nonlinear_distortion_maps_origin_exactly_and_uses_all_knots() -> None:
    optics = _optics()
    # Nonlinear three-knot map; fitting a straight line must not move the origin.
    optics["raytrace"]["geometry"]["function"][:] = np.array([[0.0], [0.4], [1.25]])
    xx, yy = np.meshgrid(np.arange(-8, 9) * 0.1, np.arange(-8, 9) * 0.1)
    cube = np.hypot(xx, yy)[:, :, None]
    result = op._raytrace_geometry(cube, np.array([550.0]), optics, _scene(cube))
    assert result[8, 8, 0] == pytest.approx(0.0, abs=1e-14)
    assert result[8, 12, 0] == pytest.approx(0.6, abs=1e-12)
    assert result[8, 10, 0] == pytest.approx(0.3, abs=1e-12)


def test_distortion_and_illumination_have_independent_field_grids() -> None:
    optics = _optics()
    ri = optics["raytrace"]["relative_illumination"]
    ri["field_height_mm"] = np.array([0.0, 0.3, 0.6, 1.5])
    ri["function"] = np.repeat((1 - 0.2 * ri["field_height_mm"])[:, None], 2, axis=1)
    cube = np.ones((17, 17, 1))
    result = op._raytrace_geometry(cube, np.array([550.0]), optics, _scene(cube))
    assert result[8, 8, 0] == pytest.approx(1.0)
    assert result[8, 13, 0] == pytest.approx(0.88)


def test_psf_positions_use_own_field_grid_and_wavelength_distortion() -> None:
    optics = _optics()
    optics["raytrace"]["psf"]["field_height_mm"] = np.array([0.0, 0.3, 1.5])
    optics["raytrace"]["geometry"]["function"][:, 1] = np.array([0.0, 0.6, 1.4])
    cache = _precompute(optics, (500.0, 600.0))
    assert cache["img_height_mm_by_wavelength"][1, 0] == pytest.approx(0.25)
    assert cache["img_height_mm_by_wavelength"][1, 1] == pytest.approx(0.3)


def test_first_noncentral_anisotropic_psf_rotates_by_ninety_degrees() -> None:
    optics = _optics()
    kernels = optics["raytrace"]["psf"]["function"]
    kernels[:, :, 1, :] = 0.0
    kernels[2, 1:4, 1, :] = np.array([0.25, 0.5, 0.25])[:, None]
    cache = _precompute(optics)
    horizontal = cache["psf"][0, 1, 0]
    vertical = cache["psf"][1, 1, 0]
    np.testing.assert_allclose(vertical, np.rot90(horizontal), atol=1e-7)
    assert not np.allclose(vertical, horizontal)


def test_spectral_psf_interpolation_normalizes_sources_before_mixing() -> None:
    optics = _optics()
    kernels = optics["raytrace"]["psf"]["function"]
    kernels[:, :, :, 0] = 0.0
    kernels[:, :, :, 1] = 0.0
    kernels[2, 1, :, 0] = 20.0
    kernels[2, 3, :, 1] = 1.0
    psf = op.rt_psf_interp(optics, 0.0, 0.0, 550.0)
    assert psf[2, 1] == pytest.approx(0.5)
    assert psf[2, 3] == pytest.approx(0.5)
    assert psf.sum() == pytest.approx(1.0)


def test_geometry_and_ri_interpolate_wavelength_inside_coverage() -> None:
    optics = _optics()
    optics["raytrace"]["geometry"]["function"][:, 1] *= 1.2
    optics["raytrace"]["relative_illumination"]["function"][:, 1] = 0.8
    np.testing.assert_allclose(op.rt_di_interp(optics, 550), [0, 0.55, 1.375])
    np.testing.assert_allclose(op.rt_ri_interp(optics, 550), 0.9)


@pytest.mark.parametrize("wavelength", [499.0, 601.0, np.nan])
def test_strict_spectral_domain_is_not_silently_clamped(wavelength: float) -> None:
    with pytest.raises(ValueError, match="coverage"):
        op.rt_psf_interp(_optics(), wavelength_nm=wavelength)


@pytest.mark.parametrize("table", ["psf", "geometry", "relative_illumination"])
def test_unsorted_wavelength_axes_are_rejected(table: str) -> None:
    optics = _optics()
    optics["raytrace"][table]["wavelength_nm"] = np.array([600.0, 500.0])
    with pytest.raises(ValueError, match="strictly increasing"):
        op.validate_raytrace_contract(optics)


@pytest.mark.parametrize("size", [5, 6])
def test_odd_and_even_source_centers_do_not_shift_an_impulse(size: int) -> None:
    optics = _optics()
    kernel = np.zeros((size, size, 3, 2))
    if size % 2:
        kernel[size // 2, size // 2] = 1.0
    else:
        kernel[size // 2 - 1 : size // 2 + 1, size // 2 - 1 : size // 2 + 1] = 0.25
    optics["raytrace"]["psf"]["function"] = kernel
    cache = _precompute(optics)
    cube = np.zeros((17, 17, 1))
    cube[8, 12, 0] = 1.0
    result = op._raytrace_apply_psf(cube, cache, 0.0001, pad_pixels=(4, 4))[:, :, 0]
    rr, cc = np.indices(result.shape)
    assert result.sum() == pytest.approx(1.0, abs=1e-7)
    assert (result * rr).sum() == pytest.approx(12.0, abs=1e-6)
    assert (result * cc).sum() == pytest.approx(16.0, abs=1e-6)


def test_requested_scene_distance_is_rejected_without_mutation() -> None:
    optics = _optics()
    scene = _scene(distance=2.0)
    oi = OpticalImage(fields={"optics": optics})
    with pytest.raises(ValueError, match="distance"):
        op.oi_compute(oi, scene)
    assert scene.fields["distance_m"] == 2.0


@pytest.mark.parametrize("bad", ["empty", "zero", "negative", "nan"])
def test_invalid_psf_arrays_are_rejected(bad: str) -> None:
    optics = _optics()
    psf = optics["raytrace"]["psf"]
    if bad == "empty":
        psf["function"] = np.empty((0, 0, 3, 2))
    elif bad == "zero":
        psf["function"][:] = 0.0
    elif bad == "negative":
        psf["function"][0, 0, 1, 0] = -0.1
    else:
        psf["function"][0, 0, 1, 0] = np.nan
    with pytest.raises(ValueError):
        op.validate_raytrace_contract(optics)


def test_legacy_nearest_wavelength_and_distance_behavior_remain_explicitly_legacy() -> None:
    optics = deepcopy(_optics())
    optics["raytrace"].pop("contract_version")
    optics["raytrace"].pop("physics_policy")
    np.testing.assert_allclose(
        op.rt_di_interp(optics, 900), optics["raytrace"]["geometry"]["function"][:, -1]
    )
    scene = _scene(distance=2.0)
    _, prepared = op._prepare_raytrace_scene(OpticalImage(fields={"optics": optics}), scene)
    assert prepared.fields["distance_m"] == np.inf
    assert scene.fields["distance_m"] == 2.0


def test_contract_metadata_survives_raw_optics_and_psf_cache_roundtrip() -> None:
    optics = _optics()
    restored = op._normalize_raytrace_optics(op._export_optics(optics))
    assert restored["raytrace"]["contract_version"] == 2
    assert restored["raytrace"]["psf"]["field_coordinate"] == "ideal_image_height"
    cache = _precompute(optics)
    recovered = op._normalize_psf_struct(op._export_psf_struct(cache))
    assert recovered["contract_version"] == 2
    np.testing.assert_equal(
        recovered["img_height_mm_by_wavelength"], cache["img_height_mm_by_wavelength"]
    )


def test_strict_cached_psfs_are_invalidated_when_lens_data_changes() -> None:
    optics = _optics()
    cache = _precompute(optics)
    args = (np.array([550.0]), 17, 17, 0.0001, np.arange(0.0, 361.0, 90.0))
    assert op._raytrace_psf_struct_matches(cache, optics, *args)
    optics["raytrace"]["psf"]["function"][2, 1, 1, 0] = 0.2
    assert not op._raytrace_psf_struct_matches(cache, optics, *args)


def test_strict_oi_compute_matches_explicit_irradiance_geometry_psf_stages() -> None:
    optics = _optics()
    scene = _scene()
    oi = OpticalImage(fields={"optics": optics, "psf_angle_step_deg": 90.0})
    computed = op.oi_compute(oi, scene)
    cache = computed.fields["psf_struct"]
    irradiance = op._radiance_to_irradiance(scene.data["photons"], optics, scene)
    geometry = op._raytrace_geometry(irradiance, scene.fields["wave"], optics, scene)
    expected = op._raytrace_apply_psf(
        geometry, cache, 0.0001, pad_pixels=op._raytrace_padding_pixels(cache)
    )
    np.testing.assert_allclose(computed.data["photons"], expected, atol=1e-14)
    assert computed.data["photons"][12, 12, 0] == pytest.approx(irradiance[8, 8, 0])


def test_bounded_strict_psf_matches_precomputed_field_and_angle_interpolation() -> None:
    optics = _optics()
    psf = optics["raytrace"]["psf"]["function"]
    psf[:, :, 1:, :] = 0.0
    psf[2, 1:4, 1, :] = np.array([0.2, 0.6, 0.2])[:, None]
    psf[1:4, 2, 2, :] = np.array([0.2, 0.6, 0.2])[:, None]
    rng = np.random.default_rng(17)
    scene = _scene(rng.uniform(0.1, 2.0, (17, 17, 1)))
    fields = {"optics": optics, "psf_angle_step_deg": 90.0}
    ordinary = op.oi_compute(OpticalImage(fields=fields), scene)
    bounded = op.oi_compute(
        OpticalImage(fields={**fields, "psf_compute_mode": "bounded_streaming"}), scene
    )
    np.testing.assert_allclose(
        bounded.data["photons"], ordinary.data["photons"], rtol=1e-7, atol=1e-12
    )
    assert bounded.fields["padding_pixels"] == ordinary.fields["padding_pixels"]
    assert bounded.fields["psf_struct"] is None
    assert bounded.fields["psf_streaming"]["peak_cached_kernels"] <= 4
    assert bounded.fields["psf_streaming"]["complete_precomputed_stack"] is False
    np.testing.assert_allclose(
        op.oi_get(bounded, "psf wavelength"), op.oi_get(ordinary, "psf wavelength")
    )
    np.testing.assert_allclose(
        op.oi_get(bounded, "psf image heights"), op.oi_get(ordinary, "psf image heights")
    )
    assert op.oi_get(bounded, "rt psf size") == op.oi_get(ordinary, "rt psf size")


def test_bounded_strict_psf_tile_retains_global_field_and_azimuth() -> None:
    optics = _optics()
    psf = optics["raytrace"]["psf"]["function"]
    psf[:, :, 1:, :] = 0.0
    psf[2, 1:4, 1, 0] = [0.2, 0.6, 0.2]
    psf[1:4, 2, 2, 0] = [0.2, 0.6, 0.2]
    psf[1:4, 2, 1, 1] = [0.1, 0.8, 0.1]
    psf[2, 1:4, 2, 1] = [0.1, 0.8, 0.1]
    rng = np.random.default_rng(41)
    cube = rng.uniform(0.1, 2.0, (33, 37, 2))
    wave = np.array([500.0, 600.0])
    angles = np.arange(0.0, 361.0, 90.0)
    padding = (4, 4)
    full, _ = op._strict_raytrace_apply_psf_bounded(
        cube, optics, wave, 0.0001, padding, angles
    )

    origin = (8, 10)
    tile, execution = op._strict_raytrace_apply_psf_bounded(
        cube[8:25, 10:28], optics, wave, 0.0001, padding, angles,
        global_grid_shape_row_col=(33, 37), input_origin_row_col=origin,
    )
    # A five-cell PSF has radius two. Keep four input cells around the scored
    # core so this is a true convolution comparison, not a boundary artifact.
    np.testing.assert_allclose(
        tile[padding[0] + 4:padding[0] + 13, padding[1] + 4:padding[1] + 14],
        full[padding[0] + 12:padding[0] + 21, padding[1] + 14:padding[1] + 24],
        rtol=1e-6, atol=1e-12,
    )
    assert execution["global_grid_shape_row_col"] == [33, 37]
    assert execution["input_origin_row_col"] == [8, 10]
    with pytest.raises(ValueError, match="both global grid shape and origin"):
        op._strict_raytrace_apply_psf_bounded(
            cube[:10, :10], optics, wave, 0.0001, padding, angles,
            global_grid_shape_row_col=(33, 37),
        )
    with pytest.raises(ValueError, match="outside"):
        op._strict_raytrace_apply_psf_bounded(
            cube[:10, :10], optics, wave, 0.0001, padding, angles,
            global_grid_shape_row_col=(33, 37), input_origin_row_col=(30, 30),
        )


def test_strict_source_tiles_reconstruct_an_offcenter_optical_roi() -> None:
    optics = _optics()
    psf = optics["raytrace"]["psf"]["function"]
    psf[:, :, 1:, :] = 0.0
    psf[2, 1:4, 1, 0] = [0.2, 0.6, 0.2]
    psf[1:4, 2, 2, 0] = [0.2, 0.6, 0.2]
    psf[1:4, 2, 1, 1] = [0.1, 0.8, 0.1]
    psf[2, 1:4, 2, 1] = [0.1, 0.8, 0.1]
    rng = np.random.default_rng(43)
    cube = rng.uniform(0.1, 2.0, (17, 19, 2))
    wave = np.array([500.0, 600.0])
    angles = np.arange(0.0, 361.0, 90.0)
    padding = (4, 4)
    full, _ = op._strict_raytrace_apply_psf_bounded(
        cube, optics, wave, 0.0001, padding, angles
    )
    roi = (3, 14, 4, 17)
    tiled, evidence = op._strict_raytrace_apply_psf_bounded_roi(
        cube, optics, wave, 0.0001, padding, angles,
        output_roi_row_col=roi, source_tile_shape_row_col=(5, 6),
    )
    np.testing.assert_allclose(
        tiled,
        full[padding[0] + roi[0]:padding[0] + roi[1],
             padding[1] + roi[2]:padding[1] + roi[3]],
        rtol=1e-6, atol=1e-12,
    )
    assert evidence["source_tiles_processed"] > 1
    assert evidence["global_field_coordinates_preserved"] is True
    assert evidence["full_frame_execution_qualified"] is False
    with pytest.raises(ValueError, match="full sampled PSF"):
        op._strict_raytrace_apply_psf_bounded_roi(
            cube, optics, wave, 0.0001, (1, 1), angles,
            output_roi_row_col=roi,
        )


def test_source_chunks_smaller_than_psf_still_contribute_across_tile_seams() -> None:
    optics = _optics()
    kernel = np.zeros((13, 13, 3, 2))
    kernel[6, 6, :, :] = 0.4
    kernel[0, 0, :, :] = 0.2
    kernel[12, 12, :, :] = 0.2
    kernel[0, 12, :, :] = 0.1
    kernel[12, 0, :, :] = 0.1
    optics["raytrace"]["psf"]["function"] = kernel
    cube = np.zeros((17, 19, 1))
    cube[2, 3, 0] = 1.0
    cube[14, 15, 0] = 2.0
    wave = np.array([550.0])
    angles = np.arange(0.0, 361.0, 90.0)
    padding = (8, 8)
    full, _ = op._strict_raytrace_apply_psf_bounded(
        cube, optics, wave, 0.0001, padding, angles
    )
    roi = (5, 13, 6, 14)
    tiled, evidence = op._strict_raytrace_apply_psf_bounded_roi(
        cube, optics, wave, 0.0001, padding, angles,
        output_roi_row_col=roi, source_tile_shape_row_col=(4, 4),
    )
    np.testing.assert_allclose(
        tiled,
        full[padding[0] + roi[0]:padding[0] + roi[1],
             padding[1] + roi[2]:padding[1] + roi[3]],
        rtol=1e-6, atol=1e-12,
    )
    assert tiled.max() > 0.0
    assert evidence["source_tiles_processed"] > 4


def test_on_demand_distorted_tile_provider_matches_full_optical_roi() -> None:
    optics = _optics()
    kernel = np.asarray(optics["raytrace"]["psf"]["function"])
    kernel[1, 2, :, :] += 0.25
    kernel[3, 1, :, :] += 0.15
    rng = np.random.default_rng(207)
    full_source = rng.uniform(0.1, 1.0, (65, 67, 2))
    wave = np.array([500.0, 600.0])
    angles = np.arange(0.0, 361.0, 90.0)
    padding = (4, 4)
    roi = (27, 39, 28, 40)
    full, _ = op._strict_raytrace_apply_psf_bounded(
        full_source, optics, wave, 0.0001, padding, angles
    )
    calls: list[tuple[int, int, int, int]] = []

    def read_tile(r0: int, r1: int, c0: int, c1: int) -> np.ndarray:
        calls.append((r0, r1, c0, c1))
        return full_source[r0:r1, c0:c1]

    streamed, evidence = op._strict_raytrace_apply_psf_bounded_roi_provider(
        (65, 67), read_tile, optics, wave, 0.0001, padding, angles,
        output_roi_row_col=roi,
        source_identity="pinned-analytic-test-207",
        source_tile_shape_row_col=(8, 8),
    )
    np.testing.assert_allclose(
        streamed,
        full[padding[0] + roi[0]:padding[0] + roi[1],
             padding[1] + roi[2]:padding[1] + roi[3]],
        rtol=1e-6, atol=1e-12,
    )
    assert evidence["full_source_cube_allocated_by_operator"] is False
    assert evidence["caller_source_cube_residency"] == "unknown"
    assert evidence["caller_source_memory_qualified"] is False
    assert evidence["source_identity"] == "pinned-analytic-test-207"
    assert len(calls) == evidence["source_tiles_processed"]
    assert len(calls) < 9 * 9
    with pytest.raises(ValueError, match="unexpected spectral shape"):
        op._strict_raytrace_apply_psf_bounded_roi_provider(
            (65, 67), lambda r0, r1, c0, c1: np.zeros((r1 - r0, c1 - c0, 1)),
            optics, wave, 0.0001, padding, angles,
            output_roi_row_col=roi, source_identity="broken-shape",
        )


def test_roi_psf_preparation_cache_retains_exact_multiband_field_result(monkeypatch) -> None:
    optics = _optics()
    psf = optics["raytrace"]["psf"]["function"]
    psf[:, :, :, :] = 0.0
    for field in range(3):
        for band in range(2):
            psf[2, 2, field, band] = 1.0
            psf[1 + field, 1 + band, field, band] += 0.25 + 0.1 * field + 0.05 * band
    source = np.random.default_rng(508).uniform(0.1, 1.0, (65, 67, 2))
    wave = np.array([500.0, 600.0])
    angles = np.arange(0.0, 361.0, 90.0)
    kwargs = {
        "output_roi_row_col": (27, 39, 28, 40),
        "source_identity": "cache-parity-multiband-field",
        "source_tile_shape_row_col": (8, 8),
    }

    def run() -> tuple[np.ndarray, dict]:
        return op._strict_raytrace_apply_psf_bounded_roi_provider(
            source.shape[:2], lambda r0, r1, c0, c1: source[r0:r1, c0:c1],
            optics, wave, 0.0001, (4, 4), angles, **kwargs,
        )

    cached, cached_evidence = run()
    monkeypatch.setattr(op, "_STRICT_ROI_PSF_CACHE_BUDGET_BYTES", 0)
    uncached, uncached_evidence = run()
    np.testing.assert_array_equal(cached, uncached)
    assert cached_evidence["shared_psf_preparation_cache_kernel_hits"] > 0
    assert cached_evidence["generated_kernel_count"] < uncached_evidence["generated_kernel_count"]
    assert (
        cached_evidence["shared_psf_preparation_cache_peak_bytes"]
        <= cached_evidence["shared_psf_preparation_cache_budget_bytes"]
    )
    assert uncached_evidence["shared_psf_preparation_cache_peak_bytes"] == 0


def test_prevalidated_roi_matches_conservative_tiles_and_scans_psf_once(monkeypatch) -> None:
    optics = _optics()
    source = np.random.default_rng(990).uniform(0.1, 1.0, (33, 35, 2))
    wave = np.array([500.0, 600.0])
    angles = np.arange(0.0, 361.0, 90.0)
    kwargs = {
        "output_roi_row_col": (12, 21, 12, 23),
        "source_identity": "prepared-psf-parity",
        "source_tile_shape_row_col": (8, 8),
    }

    def read_tile(r0, r1, c0, c1):
        return source[r0:r1, c0:c1]

    reference, _ = op._strict_raytrace_apply_psf_bounded_roi_provider(
        source.shape[:2], read_tile, optics, wave, 0.0001, (4, 4), angles, **kwargs,
    )
    validation = op._validate_strict_raytrace_optics
    signature = op._strict_raytrace_cache_signature
    counts = {"validation": 0, "signature": 0}

    def counted_validation(value):
        counts["validation"] += 1
        return validation(value)

    def counted_signature(value):
        counts["signature"] += 1
        return signature(value)

    monkeypatch.setattr(op, "_validate_strict_raytrace_optics", counted_validation)
    monkeypatch.setattr(op, "_strict_raytrace_cache_signature", counted_signature)
    prepared, evidence = op._strict_raytrace_apply_psf_bounded_roi_provider(
        source.shape[:2], read_tile, optics, wave, 0.0001, (4, 4), angles,
        _validated_optics_once_per_roi=True, **kwargs,
    )
    np.testing.assert_array_equal(prepared, reference)
    assert evidence["source_tiles_processed"] > 1
    assert evidence["strict_psf_validation_once_per_roi"] is True
    assert counts == {"validation": 1, "signature": 2}


def test_prevalidated_roi_discards_result_if_optics_changes_during_tiles() -> None:
    optics = _optics()
    source = np.ones((33, 35, 2), dtype=float)
    touched = False

    def read_tile(r0, r1, c0, c1):
        nonlocal touched
        if not touched:
            optics["raytrace"]["psf"]["function"][2, 2, 0, 0] += 0.25
            touched = True
        return source[r0:r1, c0:c1]

    with pytest.raises(ValueError, match="changed during prepared PSF convolution"):
        op._strict_raytrace_apply_psf_bounded_roi_provider(
            source.shape[:2], read_tile, optics, np.array([500.0, 600.0]),
            0.0001, (4, 4), np.arange(0.0, 361.0, 90.0),
            output_roi_row_col=(12, 21, 12, 23),
            source_identity="prepared-psf-mutation",
            source_tile_shape_row_col=(8, 8),
            _validated_optics_once_per_roi=True,
        )
    assert touched


def test_roi_psf_preparation_cache_enforces_byte_budget_and_context() -> None:
    optics = _optics()
    wave = np.array([500.0, 600.0])
    angles = np.arange(0.0, 361.0, 90.0)
    signature = op._strict_raytrace_cache_signature(optics)
    cache = op._StrictROIPSFPreparationCache(signature, wave, angles, 0.0001, 64)
    first = np.full((4, 4), 1.0, dtype=np.float32)
    second = np.full((4, 4), 2.0, dtype=np.float32)
    cache.put((0, 0, 0), first)
    cache.put((1, 0, 0), second)
    assert cache.get((0, 0, 0)) is None
    assert cache.get((1, 0, 0)) is second
    assert cache.resident_bytes == cache.peak_resident_bytes == cache.max_bytes == 64
    assert cache.evictions == 1
    cache.put((0, 1, 0), np.ones((5, 5), dtype=np.float32))
    assert cache.resident_bytes == 64
    with pytest.raises(ValueError, match="cannot cross"):
        cache.validate(signature, wave[::-1], angles, 0.0001)
    with pytest.raises(ValueError, match="cannot cross"):
        cache.validate(signature, wave, angles, 0.0002)
    optics["raytrace"]["psf"]["function"][2, 2, 0, 0] += 0.1
    with pytest.raises(ValueError, match="cannot cross"):
        cache.validate(op._strict_raytrace_cache_signature(optics), wave, angles, 0.0001)


def test_roi_psf_preparation_cache_releases_no_benefit_working_set(monkeypatch) -> None:
    optics = _optics()
    source = np.random.default_rng(509).uniform(0.1, 1.0, (65, 67, 2))
    kwargs = {
        "output_roi_row_col": (27, 39, 28, 40),
        "source_identity": "cache-no-benefit-fallback",
        "source_tile_shape_row_col": (8, 8),
    }

    def run() -> tuple[np.ndarray, dict]:
        return op._strict_raytrace_apply_psf_bounded_roi_provider(
            source.shape[:2], lambda r0, r1, c0, c1: source[r0:r1, c0:c1],
            optics, np.array([500.0, 600.0]), 0.0001, (4, 4),
            np.arange(0.0, 361.0, 90.0), **kwargs,
        )

    monkeypatch.setattr(op, "_STRICT_ROI_PSF_CACHE_BUDGET_BYTES", 0)
    uncached, _ = run()
    monkeypatch.setattr(op, "_STRICT_ROI_PSF_CACHE_BUDGET_BYTES", 32)
    monkeypatch.setattr(op._StrictROIPSFPreparationCache, "get", lambda _self, _key: None)
    fallback, evidence = run()
    np.testing.assert_array_equal(fallback, uncached)
    assert evidence["source_tiles_processed"] > 8
    assert evidence["shared_psf_preparation_cache_auto_disabled_after_tiles"] == 8
    assert evidence["shared_psf_preparation_cache_evictions"] > 0
    assert evidence["shared_psf_preparation_cache_peak_bytes"] <= 32


def test_roi_psf_preparation_cache_accepts_only_explicit_bounded_cap() -> None:
    optics = _optics()
    source = np.random.default_rng(510).uniform(0.1, 1.0, (33, 35, 2))
    kwargs = {
        "output_roi_row_col": (12, 21, 12, 23),
        "source_identity": "explicit-cache-cap-test",
        "source_tile_shape_row_col": (8, 8),
    }

    def run(cap: int) -> tuple[np.ndarray, dict]:
        return op._strict_raytrace_apply_psf_bounded_roi_provider(
            source.shape[:2], lambda r0, r1, c0, c1: source[r0:r1, c0:c1],
            optics, np.array([500.0, 600.0]), 0.0001, (4, 4),
            np.arange(0.0, 361.0, 90.0),
            shared_psf_cache_budget_bytes=cap, **kwargs,
        )

    uncached, off = run(0)
    cached, on = run(64 * 1024**2)
    np.testing.assert_array_equal(cached, uncached)
    assert off["shared_psf_preparation_cache_budget_bytes"] == 0
    assert on["shared_psf_preparation_cache_budget_bytes"] == 64 * 1024**2
    assert on["shared_psf_preparation_cache_peak_bytes"] <= 64 * 1024**2
    with pytest.raises(ValueError, match="bounded cache policy"):
        run(257 * 1024**2)


def test_virtual_large_grid_roi_visits_only_intersecting_source_tiles(monkeypatch) -> None:
    optics = _optics()
    calls: list[tuple[int, int, int, int]] = []

    def read_tile(r0: int, r1: int, c0: int, c1: int) -> np.ndarray:
        calls.append((r0, r1, c0, c1))
        return np.ones((r1 - r0, c1 - c0, 1), dtype=float)

    def bounded(source, _optics, _wave, _spacing, _padding, _angles, **_kwargs):
        return source, {
            "nonzero_input_convolutions": 1,
            "generated_kernel_count": 1,
            "peak_cached_kernels": 1,
        }

    monkeypatch.setattr(op, "_raytrace_expected_kernel_shape", lambda *_args: (1, 1))
    monkeypatch.setattr(op, "_strict_raytrace_apply_psf_bounded", bounded)
    roi = (500_000_000, 500_000_002, 500_000_000, 500_000_002)
    result, evidence = op._strict_raytrace_apply_psf_bounded_roi_provider(
        (1_000_000_000, 1_000_000_000),
        read_tile,
        optics,
        np.array([550.0]),
        1e-6,
        (0, 0),
        np.arange(0.0, 361.0, 90.0),
        output_roi_row_col=roi,
        source_identity="virtual-grid-traversal-test",
        source_tile_shape_row_col=(16, 16),
    )
    np.testing.assert_array_equal(result, np.ones((2, 2, 1)))
    assert evidence["source_tiles_processed"] == len(calls) == 1


def test_bounded_streaming_rejects_non_strict_optics_before_psf_work() -> None:
    optics = _optics()
    optics["raytrace"].pop("contract_version", None)
    optics["raytrace"].pop("physics_policy", None)
    with pytest.raises(ValueError, match="requires strict ray-trace"):
        op.oi_compute(
            OpticalImage(fields={"optics": optics, "psf_compute_mode": "bounded_streaming"}),
            _scene(),
        )


def test_bounded_psf_metadata_keeps_field_count_with_multiple_wavelengths() -> None:
    optics = _optics()
    scene = _scene(np.ones((17, 17, 2)))
    scene.fields["wave"] = np.array([500.0, 600.0])
    fields = {"optics": optics, "psf_angle_step_deg": 90.0}
    ordinary = op.oi_compute(OpticalImage(fields=fields), scene)
    bounded = op.oi_compute(
        OpticalImage(fields={**fields, "psf_compute_mode": "bounded_streaming"}), scene
    )
    np.testing.assert_allclose(
        op.oi_get(bounded, "psf image heights"), op.oi_get(ordinary, "psf image heights")
    )
    assert op.oi_get(bounded, "psf image heights n") == 3
    assert op.oi_get(bounded, "psf wavelength n") == 2


@pytest.mark.parametrize(
    ("shape", "kernel_shape", "padding"),
    [((17, 31), (5, 7), (4, 8)), ((17, 17), (51, 53), (27, 29)),
     ((8, 8), (3, 3), (0, 0)), ((32, 32), (783, 783), (400, 400))],
)
def test_sparse_input_full_fft_matches_padded_same_convolution(
    shape: tuple[int, int], kernel_shape: tuple[int, int], padding: tuple[int, int]
) -> None:
    rng = np.random.default_rng(6)
    weighted = rng.normal(size=shape) * (rng.random(shape) < 0.05)
    kernel = rng.random(kernel_shape)
    kernel /= kernel.sum()
    expected = op.fftconvolve(
        np.pad(weighted, ((padding[0], padding[0]), (padding[1], padding[1]))),
        kernel, mode="same",
    )
    actual = np.zeros_like(expected)
    assert op._strict_raytrace_sparse_convolution_same(
        weighted, kernel, padding, actual
    ) is True
    np.testing.assert_allclose(actual, expected, rtol=1e-11, atol=1e-12)
    empty = np.zeros_like(actual)
    assert op._strict_raytrace_sparse_convolution_same(
        np.zeros_like(weighted), kernel, padding, empty
    ) is False
    assert not np.any(empty)


def test_strict_public_support_and_grid_are_centered_in_declared_coordinates() -> None:
    optics = _optics()
    oi = OpticalImage(
        fields={"optics": optics, "sample_spacing_m": 0.0001},
        data={"photons": np.ones((17, 17, 1))},
    )
    x, y = op._raw_raytrace_psf_support_axes(oi, "mm")
    np.testing.assert_allclose(x, [-0.2, -0.1, 0.0, 0.1, 0.2])
    np.testing.assert_allclose(y, [0.2, 0.1, 0.0, -0.1, -0.2])
    xx, yy, _ = op.rt_psf_grid(oi, "mm")
    assert xx[xx.shape[0] // 2, xx.shape[1] // 2] == 0.0
    assert yy[yy.shape[0] // 2, yy.shape[1] // 2] == 0.0


def test_nonmonotone_distortion_is_rejected() -> None:
    optics = _optics()
    optics["raytrace"]["geometry"]["function"][:] = np.array([[0.0], [1.0], [0.5]])
    with pytest.raises(ValueError, match="monotone"):
        op.validate_raytrace_contract(optics)


def test_unknown_radiometry_requires_explicit_assumption_acceptance() -> None:
    optics = _optics()
    optics["raytrace"]["radiometry_status"] = "unknown"
    with pytest.raises(ValueError, match="radiometry"):
        op.validate_raytrace_contract(optics)
    optics["raytrace"]["radiometry_assumption_accepted"] = True
    op.validate_raytrace_contract(optics)
