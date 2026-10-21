"""Planning-only tests for global-coordinate strict Lens x Sensor tile support."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

import camera_platform.lens_sensor_tile_plan as tile_plan
from camera_platform.lens_sensor_tile_plan import plan_strict_scene_tile
from pyisetcam.optics import (
    _strict_raytrace_apply_psf_bounded,
    _strict_raytrace_geometry,
)
from pyisetcam.types import Scene

WORKSPACE = Path(__file__).resolve().parents[2]
P0014_REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"
DEF_2512_801_RUN = "360dfe5370ada20b81c085d5859e9b0ae54be66df0a5e7e8eed0bffe7c6dc601"


def _optics(*, kernel: int = 5, distortion: float = 1.0) -> dict:
    fields = np.array([0.0, 0.1])
    waves = np.array([500.0, 600.0])

    def table(values: np.ndarray) -> dict:
        return {
            "field_height_mm": fields.copy(),
            "wavelength_nm": waves.copy(),
            "function": values,
            "field_coordinate": "ideal_image_height",
        }

    psf = np.zeros((kernel, kernel, 2, 2))
    psf[kernel // 2, kernel // 2, :, :] = 1.0
    psf_table = table(psf)
    psf_table.update(
        {
            "sample_spacing_mm": np.array([0.001, 0.001]),
            "coordinate_convention": "pixel_centers",
            "row_axis": "y_up",
            "psf_reference_axis": "+x",
        }
    )
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
            "max_fov_deg": 10.0,
            "geometry": table(np.array([[0.0, 0.0], [0.1 / distortion, 0.1 / distortion]])),
            "relative_illumination": table(np.ones((2, 2))),
            "psf": psf_table,
        },
    }


def _sensor() -> dict:
    return {
        "status": "executed_operator_parameters_recorded",
        "native_pixel_sample_pitch_um_row_col": [1.0, 1.0],
        "oi_pitch_um_row_col": [1.0, 1.0],
        "gaussian_kernel_shape_row_col": [1, 1],
        "interpolation": "bilinear_constant_zero",
        "n_samples_per_pixel": 1,
        "mosaic": True,
        "ocl_group_proxy_mode": "off",
        "pixel_aperture_already_in_optical_psf": False,
        "fdtd_sensor_attached": False,
        "tcad_sensor_attached": False,
        "fdtd_sensor_config": None,
        "tcad_sensor_config": None,
        "physical_estimate_config": None,
        "fdtd_estimate_config": None,
    }


def _isp() -> dict:
    return {
        "implementation_id": "fixed-test-isp-v1",
        "halo_native_pixels_row_col": [1, 1],
        "boundary_mode": "constant_zero",
    }


def _request(*, shape: int = 64, core: tuple[int, int, int, int] = (6, 10, 6, 10)) -> dict:
    fov = math.degrees(2 * math.atan(shape * 0.001 / 20.0))
    return {
        "scene_shape_row_col": [shape, shape],
        "scene_horizontal_fov_deg": fov,
        "scene_vertical_fov_deg": fov,
        "declared_oi_pitch_um": 1.0,
        "source_scene_kind": "analytic_continuous",
        "source_scene_id": "half-plane-fixture",
        "native_shape_row_col": [16, 16],
        "native_core_row_col_exclusive": list(core),
        "native_pixel_pitch_um_row_col": [1.0, 1.0],
        "sensor_operator": _sensor(),
        "isp_operator": _isp(),
        "wavelengths_nm": [500.0, 600.0],
    }


def test_centered_tile_includes_pixel_isp_psf_and_strict_geometry_halo() -> None:
    result = plan_strict_scene_tile(_optics(), **_request())
    assert result["status"] == "planning_only"
    assert result["native_isp_extended_row_col_exclusive"] == [5, 11, 5, 11]
    assert result["sensor_mosaic_integration_radius_oi_samples_row_col"] == [1, 1]
    assert result["optics"]["full_sampled_kernel_row_col"] == [5, 5]
    assert result["required_pregeometry_oi_row_col_exclusive"] == [26, 38, 26, 38]
    # The planner retains a one-sample floating-boundary safety margin.
    assert result["required_source_scene_row_col_exclusive"] == [25, 39, 25, 39]
    assert result["declared_source_support_fits"] is True
    assert result["tile_memory_policy"]["fits_declared_policy"] is True
    assert result["tile_memory_policy"][
        "strict_shared_psf_preparation_cache_policy_bytes"
    ] == tile_plan._STRICT_ROI_PSF_CACHE_BUDGET_BYTES
    assert result["execution_qualified"] is False
    assert result["full_frame_execution_qualified"] is False
    assert result["tile_equivalence_qualified"] is False


def test_strict_roi_cache_residency_is_counted_in_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = (32, 32)
    with_cache = tile_plan._memory_policy(shape, shape, (101, 101), (64, 64), 2, 3)
    monkeypatch.setattr(tile_plan, "_STRICT_ROI_PSF_CACHE_BUDGET_BYTES", 0)
    without_cache = tile_plan._memory_policy(shape, shape, (101, 101), (64, 64), 2, 3)
    assert with_cache["combined_policy_estimate_bytes"] - without_cache[
        "combined_policy_estimate_bytes"
    ] == with_cache["strict_shared_psf_preparation_cache_policy_bytes"]


def test_opt_in_roi_psf_cache_cap_is_selected_and_counted() -> None:
    select = tile_plan._strict_roi_psf_cache_budget_for_declared_policy
    assert select((2059, 2059), 512 * 1024**2) == 32 * 1024**2
    assert select((2059, 2059), 6 * 1024**3) == 256 * 1024**2
    selected = select((1031, 1031), 2 * 1024**3)
    assert 32 * 1024**2 < selected < 256 * 1024**2
    source = (32, 32)
    base = tile_plan._memory_policy(source, source, (101, 101), (64, 64), 2, 3)
    opted = tile_plan._memory_policy(
        source, source, (101, 101), (64, 64), 2, 3,
        shared_psf_cache_budget_bytes=selected,
    )
    assert opted["strict_shared_psf_preparation_cache_policy_bytes"] == selected
    assert opted["combined_policy_estimate_bytes"] - base[
        "combined_policy_estimate_bytes"
    ] == selected - 32 * 1024**2
    with pytest.raises(ValueError, match="bounded policy"):
        tile_plan._memory_policy(
            source, source, (101, 101), (64, 64), 2, 3,
            shared_psf_cache_budget_bytes=257 * 1024**2,
        )


def test_offcenter_identity_geometry_keeps_global_row_orientation() -> None:
    result = plan_strict_scene_tile(_optics(), **_request(core=(2, 4, 10, 12)))
    assert result["status"] == "planning_only"
    assert result["required_source_scene_row_col_exclusive"] == [21, 33, 29, 41]
    angular = result["required_source_angular_cell_edges_deg"]
    assert angular["row_down_deg"][0] < 0 < angular["row_down_deg"][1]
    assert angular["col_right_deg"][0] < angular["col_right_deg"][1]


def test_nonlinear_geometry_expands_source_query_against_identity() -> None:
    request = _request(core=(2, 4, 10, 12))
    identity = plan_strict_scene_tile(_optics(), **request)
    stretched = plan_strict_scene_tile(_optics(distortion=1.2), **request)
    assert stretched["status"] == "planning_only"
    assert (
        stretched["required_source_scene_row_col_exclusive"][0]
        < identity["required_source_scene_row_col_exclusive"][0]
    )
    assert (
        stretched["required_source_scene_row_col_exclusive"][3]
        > identity["required_source_scene_row_col_exclusive"][3]
    )


def test_planned_source_rectangle_preserves_offcenter_strict_optical_roi() -> None:
    optics = _optics(distortion=1.2)
    psf = optics["raytrace"]["psf"]["function"]
    psf[0, 2, :, :] = 0.2
    psf[4, 2, :, :] = 0.1
    psf[2, 0, :, :] = 0.15
    psf[2, 4, :, :] = 0.1
    request = _request(core=(2, 4, 10, 12))
    plan = plan_strict_scene_tile(optics, **request)
    assert plan["status"] == "planning_only"
    rng = np.random.default_rng(45)
    original = rng.uniform(0.1, 2.0, (64, 64, 2))
    r0, r1, c0, c1 = plan["required_source_scene_row_col_exclusive"]
    restricted = np.zeros_like(original)
    restricted[r0:r1, c0:c1, :] = original[r0:r1, c0:c1, :]
    scene = Scene(
        fields={
            "fov_deg": request["scene_horizontal_fov_deg"],
            "vfov_deg": request["scene_vertical_fov_deg"],
            "distance_m": np.inf,
            "wave": np.array(request["wavelengths_nm"]),
            "rows": 64,
            "cols": 64,
        },
        data={"photons": original},
    )
    waves = np.array(request["wavelengths_nm"])
    full_geometry = _strict_raytrace_geometry(original, waves, optics, scene)
    restricted_geometry = _strict_raytrace_geometry(restricted, waves, optics, scene)
    angles = np.arange(0.0, 361.0, 90.0)
    full, _ = _strict_raytrace_apply_psf_bounded(full_geometry, optics, waves, 1e-6, (4, 4), angles)
    selected, _ = _strict_raytrace_apply_psf_bounded(
        restricted_geometry, optics, waves, 1e-6, (4, 4), angles
    )
    or0, or1, oc0, oc1 = plan["required_post_psf_oi_row_col_exclusive"]
    np.testing.assert_allclose(
        selected[4 + or0 : 4 + or1, 4 + oc0 : 4 + oc1],
        full[4 + or0 : 4 + or1, 4 + oc0 : 4 + oc1],
        rtol=1e-6,
        atol=1e-12,
    )


def test_full_kernel_envelope_blocks_insufficient_declared_scene_halo() -> None:
    result = plan_strict_scene_tile(_optics(kernel=31), **_request(shape=32))
    assert result["status"] == "blocked_declared_source_halo"
    assert result["optics"][
        "all_field_azimuth_wavelength_kernel_envelope_radius_oi_samples_row_col"
    ] == [15, 15]
    assert result["full_sampled_psf_envelope_touches_global_scene_boundary"] is True
    assert result["declared_source_support_fits"] is False


def test_missing_isp_or_sensor_operator_is_explicitly_blocked() -> None:
    request = _request()
    request["isp_operator"] = None
    result = plan_strict_scene_tile(_optics(), **request)
    assert result["status"] == "blocked_missing_operator_contracts"
    assert "fixed_isp_halo_operator" in result["missing_operator_contracts"]
    request["isp_operator"] = {"implementation_id": "fixed-test-isp-v1"}
    result = plan_strict_scene_tile(_optics(), **request)
    assert "isp_operator.halo_native_pixels_row_col" in result["missing_operator_contracts"]


def test_pitch_mismatch_and_unsupported_pixel_operator_fail_closed() -> None:
    request = _request()
    request["declared_oi_pitch_um"] = 1.1
    with pytest.raises(ValueError, match="sampling and OI convolution pitch differ"):
        plan_strict_scene_tile(_optics(), **request)
    request = _request()
    request["sensor_operator"]["n_samples_per_pixel"] = 3
    with pytest.raises(ValueError, match="not the supported digital RAW mosaic"):
        plan_strict_scene_tile(_optics(), **request)
    request = _request()
    request["sensor_operator"]["gaussian_kernel_shape_row_col"] = [2, 2]
    with pytest.raises(ValueError, match="integration kernel differs"):
        plan_strict_scene_tile(_optics(), **request)
    request = _request()
    request["sensor_operator"]["fdtd_sensor_attached"] = True
    with pytest.raises(ValueError, match="attachment and executed config disagree"):
        plan_strict_scene_tile(_optics(), **request)


def test_attached_but_disabled_sensor_luts_are_allowed_with_explicit_configs() -> None:
    request = _request()
    request["sensor_operator"].update(
        {
            "fdtd_sensor_attached": True,
            "tcad_sensor_attached": True,
            "fdtd_sensor_config": {"enabled": False, "kind": "relative_lut"},
            "tcad_sensor_config": {"enabled": False, "kind": "collection_lut"},
        }
    )
    result = plan_strict_scene_tile(_optics(), **request)
    assert result["status"] == "planning_only"
    assert result["sensor_physics_effective_state"] == {
        "fdtd_attached": True,
        "fdtd_effective_enabled": False,
        "tcad_attached": True,
        "tcad_effective_enabled": False,
    }
    request["sensor_operator"]["fdtd_sensor_config"] = {"enabled": True}
    with pytest.raises(ValueError, match="Enabled FDTD response"):
        plan_strict_scene_tile(_optics(), **request)
    request["sensor_operator"]["fdtd_sensor_config"] = {}
    with pytest.raises(ValueError, match="Enabled FDTD response"):
        plan_strict_scene_tile(_optics(), **request)


def test_unknown_current_density_models_block_until_explicitly_ruled_out() -> None:
    request = _request()
    del request["sensor_operator"]["physical_estimate_config"]
    result = plan_strict_scene_tile(_optics(), **request)
    assert result["status"] == "blocked_missing_operator_contracts"
    assert "sensor_operator.physical_estimate_config" in result["missing_operator_contracts"]
    request["sensor_operator"]["physical_estimate_config"] = {"kind": "physical_estimate_v1"}
    with pytest.raises(ValueError, match="current-density paths lack"):
        plan_strict_scene_tile(_optics(), **request)


def test_extreme_pitch_is_rejected_before_large_psf_axis_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    pitch_um = 1e-12
    fov = math.degrees(2 * math.atan(64 * pitch_um * 1e-3 / 20.0))
    request.update(
        {
            "scene_horizontal_fov_deg": fov,
            "scene_vertical_fov_deg": fov,
            "declared_oi_pitch_um": pitch_um,
            "native_pixel_pitch_um_row_col": [pitch_um, pitch_um],
        }
    )
    request["sensor_operator"]["native_pixel_sample_pitch_um_row_col"] = [pitch_um, pitch_um]
    request["sensor_operator"]["oi_pitch_um_row_col"] = [pitch_um, pitch_um]
    monkeypatch.setattr(
        tile_plan,
        "_raytrace_expected_kernel_shape",
        lambda *_args: pytest.fail("target PSF axes must not be allocated"),
    )
    result = plan_strict_scene_tile(_optics(), **request)
    assert result["status"] == "blocked_psf_axis_budget"
    assert result["execution_qualified"] is False


def test_resource_policies_block_without_claiming_peak_rss() -> None:
    result = plan_strict_scene_tile(_optics(), **_request(), policy_budget_bytes=1)
    assert result["status"] == "blocked_tile_memory_policy"
    assert result["tile_memory_policy"]["estimate_is_peak_rss_upper_bound"] is False
    result = plan_strict_scene_tile(_optics(), **_request(), max_geometry_queries=1)
    assert result["status"] == "blocked_geometry_planner_budget"
    assert "tile_memory_policy" not in result


def test_current_optical_roi_helper_domain_is_explicitly_enforced() -> None:
    request = _request(shape=32, core=(0, 2, 15, 17))
    request["native_shape_row_col"] = [32, 32]
    result = plan_strict_scene_tile(_optics(), **request)
    assert result["status"] == "blocked_current_optical_roi_helper_domain"
    assert result["required_post_psf_oi_fits_strict_output_padding"] is True
    assert result["required_post_psf_oi_within_current_roi_helper"] is False


def test_large_global_scene_is_only_planned_without_allocating_a_full_raster() -> None:
    optics = _optics()
    optics["raytrace"]["max_fov_deg"] = 40.0
    result = plan_strict_scene_tile(optics, **_request(shape=4096))
    assert result["status"] == "planning_only"
    assert result["source_scene"]["shape_row_col"] == [4096, 4096]
    assert max(result["tile_memory_policy"]["source_tile_samples_row_col"]) < 256
    assert result["execution_qualified"] is False
    assert "target_grid_analytic_geometry_raw_equivalence" in (
        result["remaining_execution_requirements"]
    )


def test_pinned_p0014_def_2512_801_1024_tile_is_planning_only() -> None:
    """Reuse a real RAW operator at its executed pitch; never execute 1024 RAW."""
    from camera_platform.lens_catalog_bridge import _asset, _revision_source
    from camera_platform.offline_assets import offline_asset_store
    from pyisetcam.camera import camera_create, camera_get
    from pyisetcam.image_sensor_db import image_sensor_db_reconcile_scenario
    from pyisetcam.lens_patents import lens_patent_raytrace_optics
    from pyisetcam.sensor_simulation_db import sensor_simulation_db_config
    from pyisetcam.system_faca import (
        _apply_physics_and_sensor_overrides,
        _configure_scenario_readout,
    )

    db_root = WORKSPACE / "camerae2e_db"
    report_path = (
        WORKSPACE
        / "reports/camera_platform/mtf-support-20260928"
        / "dev36-direct-rehistogram-vs-stored-raw.json"
    )
    if not (db_root / "catalog.sqlite").is_file() or not report_path.is_file():
        pytest.skip("Pinned Lens/Sensor DB or executed mosaic record is unavailable")
    executed = json.loads(report_path.read_text())["runs"]["same_ray_direct_rehistogram_v8"]
    assert executed["status"] == "succeeded"
    assert executed["specification"]["lens_revision_id"] == P0014_REVISION
    assert executed["sensor_revision"]["run_id"] == DEF_2512_801_RUN
    assert executed["specification"]["sensor_size"] == [16, 16]
    mosaic = dict(executed["scene"]["sampling_evidence"]["mosaic_integration"])

    # Reconstruct the pinned sensor through the same saved-model and readout
    # configurators used by RAW capture. The old evidence records attachment,
    # but not whether its FDTD/TCAD dictionaries actually ran.
    scenario = sensor_simulation_db_config(
        "DEF-2512-801", root=db_root, run_id=DEF_2512_801_RUN
    )["scenario"]
    scenario = {
        **scenario,
        "sensor": {
            **scenario.get("sensor", {}),
            "size": [16, 16],
            "integration_time": executed["specification"]["exposure_s"],
            "noise_flag": executed["specification"]["noise_flag"],
        },
    }
    scenario = image_sensor_db_reconcile_scenario(scenario)
    scenario["sensor_output_mode"] = "digital_raw"
    assets = offline_asset_store(workspace=WORKSPACE)
    with assets.bound_default(assets):
        camera, _ = _apply_physics_and_sensor_overrides(
            camera_create(asset_store=assets), scenario, assets
        )
        sensor = _configure_scenario_readout(
            camera_get(camera, "sensor"), scenario, "digital_raw"
        )
    assert sensor.metadata["sensor_simulation_db"]["result_sha256"] == executed[
        "sensor_revision"
    ]["result_sha256"]
    pixel_pitch = np.asarray(sensor.fields["pixel"]["size_m"], dtype=float) * 1e6
    np.testing.assert_allclose(mosaic["native_pixel_sample_pitch_um_row_col"], pixel_pitch)
    assert mosaic["n_samples_per_pixel"] == sensor.fields.get("n_samples_per_pixel", 1)
    assert mosaic["mosaic"] is bool(sensor.fields.get("mosaic"))
    assert mosaic["ocl_group_proxy_mode"] == (
        "off" if sensor.fields.get("ocl_group_proxy") is None else "unknown"
    )
    oi_pitch_um = float(mosaic["oi_pitch_um_row_col"][0])
    assert mosaic["oi_pitch_um_row_col"] == [oi_pitch_um, oi_pitch_um]
    assert mosaic["gaussian_kernel_shape_row_col"] == [
        max(1, math.ceil(float(value) / oi_pitch_um)) for value in pixel_pitch
    ]
    for name in ("fdtd", "tcad"):
        config = sensor.fields.get(f"{name}_sensor")
        assert mosaic[f"{name}_sensor_attached"] is (config is not None)
        assert isinstance(config, dict) and config["enabled"] is False
        mosaic[f"{name}_sensor_config"] = config
    for name in ("physical_estimate", "fdtd_estimate"):
        config = sensor.fields.get(name)
        assert config is None
        mosaic[f"{name}_config"] = config

    revision, snapshot, lens_db, _ = _revision_source(P0014_REVISION, db_root)
    simulation_id, psf_path, _, _ = _asset(revision, snapshot, lens_db, db_root)
    optics = lens_patent_raytrace_optics(
        simulation_id,
        db_path=lens_db,
        psf_dir=psf_path.parent,
        allow_approximate_radiometry=True,
    )
    assert optics["raytrace"]["asset_sha256"] == executed["lens_identity"]["asset_sha256"]
    source_psf_pitch = np.asarray(optics["raytrace"]["psf"]["sample_spacing_mm"]) * 1000
    np.testing.assert_allclose(
        source_psf_pitch,
        executed["lens_model"]["direct_same_ray_rehistogram"]["source_psf_pitch_um_row_col"],
    )

    # Keep the previously executed OI angular pitch while proposing a larger
    # source field. This is a candidate raster, not a materialized Scene.
    image_distance_mm = float(optics["raytrace"]["effective_focal_length_m"]) * 1000
    fov_deg = math.degrees(2 * math.atan(1024 * oi_pitch_um * 1e-3 / (2 * image_distance_mm)))
    request = {
        "scene_shape_row_col": [1024, 1024],
        "scene_horizontal_fov_deg": fov_deg,
        "scene_vertical_fov_deg": fov_deg,
        "declared_oi_pitch_um": oi_pitch_um,
        "source_scene_kind": "declared_raster",
        "source_scene_id": "proposed-unmaterialized-1024-same-pitch-raster",
        "native_shape_row_col": [16, 16],
        "native_core_row_col_exclusive": [6, 10, 6, 10],
        "native_pixel_pitch_um_row_col": pixel_pitch.tolist(),
        "sensor_operator": mosaic,
        # This halo is illustrative, not an assertion about a deployed ISP.
        "isp_operator": {
            "implementation_id": "illustrative-fixed-one-native-pixel-halo-v1",
            "halo_native_pixels_row_col": [1, 1],
            "boundary_mode": "reflect",
        },
        "wavelengths_nm": [450.0, 550.0, 650.0],
    }
    result = plan_strict_scene_tile(optics, **request)
    assert result["status"] == "planning_only"
    assert result["source_scene"]["shape_row_col"] == [1024, 1024]
    assert result["source_scene"]["horizontal_fov_deg"] == pytest.approx(0.9599778930073692)
    assert result["optics"]["full_sampled_kernel_row_col"] == [547, 547]
    assert result["required_source_scene_row_col_exclusive"] == [219, 805, 219, 805]
    assert result["sensor_mosaic_integration_radius_oi_samples_row_col"] == [2, 2]
    assert result["sensor_physics_effective_state"] == {
        "fdtd_attached": True,
        "fdtd_effective_enabled": False,
        "tcad_attached": True,
        "tcad_effective_enabled": False,
    }
    assert result["tile_memory_policy"]["estimate_is_peak_rss_upper_bound"] is False
    assert result["execution_qualified"] is False
    assert result["full_frame_execution_qualified"] is False
    assert result["tile_equivalence_qualified"] is False

    # A larger declared raster at unchanged optical pitch moves the same
    # central dependency rectangle, rather than increasing local PSF support.
    # This remains a plan; neither 1024 nor 2048 RAW is executed here.
    fov_2048 = math.degrees(
        2 * math.atan(2048 * oi_pitch_um * 1e-3 / (2 * image_distance_mm))
    )
    larger = plan_strict_scene_tile(
        optics,
        **{
            **request,
            "scene_shape_row_col": [2048, 2048],
            "scene_horizontal_fov_deg": fov_2048,
            "scene_vertical_fov_deg": fov_2048,
            "source_scene_id": "proposed-unmaterialized-2048-same-pitch-raster",
        },
    )
    assert larger["status"] == "planning_only"
    assert larger["source_scene"]["horizontal_fov_deg"] == pytest.approx(1.919821061)
    assert larger["optics"]["full_sampled_kernel_row_col"] == [547, 547]
    assert larger["required_source_scene_row_col_exclusive"] == [731, 1317, 731, 1317]
    assert larger["tile_memory_policy"]["combined_policy_estimate_bytes"] == (
        result["tile_memory_policy"]["combined_policy_estimate_bytes"]
    )
    assert larger["execution_qualified"] is False
