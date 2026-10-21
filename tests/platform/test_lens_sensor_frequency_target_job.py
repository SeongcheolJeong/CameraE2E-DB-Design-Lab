"""Public frequency-target job contract and one saved-model execution."""

from __future__ import annotations

import math
import os
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.lens_sensor_frequency_target_job import (
    _fixed_tangent_plane_shift_deg,
    default_spec,
    extended_default_spec,
    specification,
)
from camera_platform.lens_sensor_fullfield_study_preflight import MODE_SCHEMA
from camera_platform.registry import IntegrityError
from camera_platform.sdk import PlatformClient
from camera_platform.sensor_db_bridge import inspect_sensor_revision

WORKSPACE = Path(__file__).resolve().parents[2]
LENS_REVISION = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"
SENSOR_CODE = "DEP-2209-801"
SENSOR_RUN = "74e15e696e8d14827bc641346dc92d113a24997da6ec13efb51d9e2583a4f188"


def _mode() -> dict:
    revision = inspect_sensor_revision(
        SENSOR_CODE, root=WORKSPACE / "camerae2e_db", run_id=SENSOR_RUN
    )["revision"]
    return {
        "schema": MODE_SCHEMA,
        "mode_id": "engineering-2048x14000-zero-origin",
        "sensor_run_id": SENSOR_RUN,
        "sensor_report_code": revision["report_code"],
        "sensor_result_sha256": revision["result_sha256"],
        "active_rows": 2048,
        "active_cols": 14000,
        "pixel_pitch_um": 1.22,
        "sensor_center_offset_from_optical_axis_um_row_col": [0.0, 0.0],
        "sensor_plane": "perpendicular_to_optical_axis",
        "sensor_orientation_deg_clockwise": 0,
        "cfa_origin_row_col": [0, 0],
        "image_plane_binding": {
            "reference_plane": "saved_prescription_image_plane",
            "axial_offset_um": 0.0,
        },
    }


def _spec(mode_id: str) -> dict:
    spec = default_spec()
    roi = spec["registered_roi"]
    roi["lens_revision_id"] = LENS_REVISION
    roi["sensor_revision"] = {"identifier": SENSOR_CODE, "run_id": SENSOR_RUN}
    roi["active_mode_declaration_id"] = mode_id
    roi["sensor_roi_bounds_row_col_exclusive"] = [1000, 1008, 12000, 12008]
    roi["sensor_tile_shape_row_col"] = [4, 8]
    roi["readout_row_chunk_rows"] = 4
    roi["analytic_scene"].update(
        shape_row_col=[4096, 28000], horizontal_fov_deg=42.0, edge_offset_deg=14.0
    )
    roi["accept_approximate_lens_radiometry"] = True
    roi["acknowledge_unverified_mode_and_uniform_sensor_response"] = True
    return spec


def test_off_axis_pixel_fraction_maps_to_exact_tangent_plane_displacement():
    edge_offset_deg = 14.047293808135992
    image_distance_m = 0.014
    requested_shift_um = 1.22 * 0.25
    shift_deg = _fixed_tangent_plane_shift_deg(
        edge_offset_deg=edge_offset_deg,
        requested_shift_um=requested_shift_um,
        image_distance_m=image_distance_m,
    )
    executed_shift_um = image_distance_m * 1e6 * (
        math.tan(math.radians(edge_offset_deg + shift_deg))
        - math.tan(math.radians(edge_offset_deg))
    )
    assert executed_shift_um == pytest.approx(requested_shift_um, abs=1e-9)
    # The former on-axis angular conversion overshot by about 6% here.
    old_shift_deg = math.degrees(math.atan(
        requested_shift_um * 1e-6 / image_distance_m
    ))
    old_shift_um = image_distance_m * 1e6 * (
        math.tan(math.radians(edge_offset_deg + old_shift_deg))
        - math.tan(math.radians(edge_offset_deg))
    )
    assert old_shift_um > requested_shift_um * 1.06


def test_frequency_goal_contract_has_explicit_observable_and_guarded_psf_mode():
    spec = _spec("active_mode_declaration-" + "a" * 32)
    spec["sampling_goal"]["mode"] = "explicit"
    spec["sampling_goal"]["max_refinements"] = 0
    checked = specification(spec)
    assert checked["sampling_goal"]["observable"] == "pre_readout_mean_electrons"
    assert checked["sampling_goal"]["frequency_direction"] == "image_plane_column_x_right"
    larger = deepcopy(spec)
    larger["sampling_goal"]["optical_tile_estimated_working_budget_bytes"] = 8 * 1024**3
    assert specification(larger)["sampling_goal"][
        "optical_tile_estimated_working_budget_bytes"
    ] == 8 * 1024**3
    larger["sampling_goal"]["optical_tile_estimated_working_budget_bytes"] += 1
    with pytest.raises(ValueError, match="8 GiB"):
        specification(larger)
    invalid = deepcopy(spec)
    invalid["sampling_goal"]["evaluation_fractions_native_nyquist"] = [0.9, 1.0]
    with pytest.raises(ValueError, match="below Nyquist"):
        specification(invalid)
    invalid = deepcopy(spec)
    invalid["sampling_goal"]["psf_resolution_mode"] = "pinned_same_ray_retrace_diagnostic"
    with pytest.raises(ValueError, match="PSF target pitch"):
        specification(invalid)
    invalid = deepcopy(spec)
    invalid["registered_roi"]["capture"]["noise_flag"] = 2
    with pytest.raises(ValueError, match="noise_flag=0"):
        specification(invalid)


def test_extended_sparse_roi_is_versioned_and_explicit():
    version_one = _spec("active_mode_declaration-" + "a" * 32)
    extended = extended_default_spec()
    extended["registered_roi"] = deepcopy(version_one["registered_roi"])
    extended["sampling_goal"]["mode"] = "explicit"
    extended["sampling_goal"]["max_refinements"] = 3
    extended["sampling_goal"]["optical_tile_estimated_working_budget_bytes"] = (
        8 * 1024**3
    )
    assert specification(extended) == extended
    assert extended["schema"] != version_one["schema"]
    assert extended["sampling_goal"]["coordinate_policy"] == (
        "extended_sparse_roi_128k_v1"
    )

    version_one["sampling_goal"]["coordinate_policy"] = (
        "extended_sparse_roi_128k_v1"
    )
    with pytest.raises(ValueError, match="exact sampling_goal"):
        specification(version_one)
    missing = deepcopy(extended)
    del missing["sampling_goal"]["coordinate_policy"]
    with pytest.raises(ValueError, match="exact sampling_goal"):
        specification(missing)
    wrong = deepcopy(extended)
    wrong["sampling_goal"]["coordinate_policy"] = "registered_roi_v1"
    with pytest.raises(ValueError, match="extended frequency ROI"):
        specification(wrong)


def test_extended_template_is_separate_from_version_one(tmp_path: Path):
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get(
            "/api/v1/session"
        ).json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = transport
        original = sdk.lens_sensor_frequency_target_template()["specification"]
        extended = sdk.lens_sensor_frequency_target_extended_sparse_roi_template()[
            "specification"
        ]
        assert original == default_spec()
        assert "coordinate_policy" not in original["sampling_goal"]
        assert extended == extended_default_spec()


def test_extended_request_queues_and_reads_with_versioned_policy(tmp_path: Path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned local Lens/Sensor DB unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get(
            "/api/v1/session"
        ).json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = transport
        mode = sdk.register_lens_sensor_active_mode(SENSOR_CODE, SENSOR_RUN, _mode())
        spec = extended_default_spec()
        spec["registered_roi"] = _spec(mode["id"])["registered_roi"]
        spec["sampling_goal"]["mode"] = "explicit"
        spec["sampling_goal"]["max_refinements"] = 3
        spec["sampling_goal"]["optical_tile_estimated_working_budget_bytes"] = (
            8 * 1024**3
        )
        queued = sdk.submit_lens_sensor_frequency_target(spec)
        assert queued["status"] == "queued"
        assert queued["plugin_id"] == spec["schema"]
        assert queued["default_worker_timeout_seconds"] == 3600
        retrieved = sdk.lens_sensor_frequency_target(queued["id"])
        assert retrieved["specification"] == specification(spec)
        assert retrieved["result"] is None


def test_real_saved_model_explicit_grid_runs_and_reports_inconclusive_frequency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned local Lens/Sensor DB unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = transport
        mode = sdk.register_lens_sensor_active_mode(SENSOR_CODE, SENSOR_RUN, _mode())
        spec = _spec(mode["id"])
        spec["sampling_goal"]["mode"] = "explicit"
        spec["sampling_goal"]["max_refinements"] = 0
        assert sdk.lens_sensor_frequency_target_template()["specification"]["schema"] == (
            spec["schema"]
        )
        submitted = sdk.submit_lens_sensor_frequency_target(spec)
        assert submitted["status"] == "queued"
        assert submitted["job_kind"] == "lens_sensor_frequency_target"
        assert submitted["submission_preflight_status"] == "worker_preflight_pending"
        from camera_platform.worker import work

        work(app.state.service, once=True, timeout=120)
        completed = sdk.lens_sensor_frequency_target(submitted["id"])
        assert completed["status"] == "succeeded", completed["error"]
        assert sdk.wait_lens_sensor_frequency_target(
            submitted["id"], timeout=5, poll_interval=0.01
        )["id"] == submitted["id"]
        assert transport.get(f"/api/v1/runs/{submitted['id']}").status_code == 404
        summary = completed["result"]["summary"]
        assert summary["status"] == "inconclusive_refinement_limit"
        shift_geometry = summary["execution"]["edge_shift_geometry"]
        assert shift_geometry["executed_tangent_plane_um"] == pytest.approx(
            shift_geometry["requested_tangent_plane_um"], abs=1e-9
        )
        assert shift_geometry["distorted_local_edge_shift_qualified"] is False
        assert summary["frozen_submission_sha256"] == submitted["content_hash"]
        assert len(summary["execution"]["steps"]) == 1
        assert len(summary["execution"]["steps"][0]["mean_electrons_sha256_by_phase"]) == 2
        assert summary["execution"]["frequency_target_points_status"] == (
            "inconclusive_mosaic_channel_contract"
        )
        assert summary["physical_mtf_comparison_allowed"] is False
        assert summary["product_ranking_allowed"] is False
        assert len(completed["result"]["artifacts"]) == 1
        assert any(
            item["id"] == submitted["id"]
            for item in sdk.list_lens_sensor_frequency_target()
        )
        altered = deepcopy(completed)
        altered["result"]["summary"]["source_identities"]["lens"]["revision_sha256"] = (
            "0" * 64
        )
        from camera_platform.lens_sensor_frequency_target_job import get

        with monkeypatch.context() as guard:
            guard.setattr(app.state.service.registry, "get_run", lambda _identity: altered)
            with pytest.raises(IntegrityError, match="frozen request"):
                get(app.state.service, submitted["id"])


def test_wide_fov_adaptive_reports_policy_limit_before_false_convergence(
    tmp_path: Path,
):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned local Lens/Sensor DB unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = transport
        mode = sdk.register_lens_sensor_active_mode(SENSOR_CODE, SENSOR_RUN, _mode())
        submitted = sdk.submit_lens_sensor_frequency_target(_spec(mode["id"]))
        from camera_platform.worker import work

        work(app.state.service, once=True, timeout=120)
        completed = sdk.lens_sensor_frequency_target(submitted["id"])
        assert completed["status"] == "succeeded", completed["error"]
        summary = completed["result"]["summary"]
        assert summary["status"] == "inconclusive_refinement_policy"
        assert summary["execution"]["steps"] == []
        assert summary["sampling_plan"]["policy_is_hardware_limit"] is False
        assert len(summary["sampling_plan"]["fixed_fov_refinement_grids"]) < 3
        assert summary["physical_mtf_comparison_allowed"] is False


@pytest.mark.skipif(
    os.environ.get("CAMERA_TEST_FULL_FREQUENCY_RETRACE") != "1",
    reason="Run explicitly with pinned Lens/Sensor DB and extended numerical work",
)
def test_central_adaptive_retrace_exposes_same_grid_numeric_psf_sensitivity(
    tmp_path: Path,
):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned local Lens/Sensor DB unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = transport
        mode = sdk.register_lens_sensor_active_mode(SENSOR_CODE, SENSOR_RUN, _mode())
        spec = _spec(mode["id"])
        roi = spec["registered_roi"]
        # This second pinned prescription reproduces its stored chief rays.
        # The wide P0248 asset fails that independent source check, so it is
        # correctly reported as inconclusive instead of used as a refined PSF.
        roi["lens_revision_id"] = "6c48e967-74b4-5901-8186-ba6fafe4797b"
        roi["sensor_roi_bounds_row_col_exclusive"] = [1020, 1028, 7000, 7008]
        roi["analytic_scene"].update(
            shape_row_col=[4096, 4096], horizontal_fov_deg=2.0, edge_offset_deg=0.0
        )
        spec["sampling_goal"]["psf_resolution_mode"] = (
            "pinned_same_ray_retrace_diagnostic"
        )
        spec["sampling_goal"]["optical_tile_estimated_working_budget_bytes"] = 1024**3
        submitted = sdk.submit_lens_sensor_frequency_target(spec)
        from camera_platform.worker import work

        work(app.state.service, once=True, timeout=180)
        completed = sdk.lens_sensor_frequency_target(submitted["id"])
        assert completed["status"] == "succeeded", completed["error"]
        summary = completed["result"]["summary"]
        assert len(summary["execution"]["steps"]) >= 1
        psf = summary["psf_target_band_diagnostic"]
        assert psf["psf_source_resolution_qualified"] is False
        assert psf["physical_mtf_comparison_allowed"] is False
        assert psf["fixed_grid_camera_sensitivity"]["same_grid_camera_comparison_executed"]
        assert psf["fixed_grid_camera_sensitivity"][
            "mean_electron_full_scale_normalized_max_delta"
        ] >= 0


@pytest.mark.skipif(
    os.environ.get("CAMERA_TEST_FULL_FREQUENCY_RETRACE") != "1",
    reason="Run explicitly with pinned Lens/Sensor DB and extended numerical work",
)
def test_narrow_fov_third_grid_resource_preflight_only(tmp_path: Path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned local Lens/Sensor DB unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = transport
        mode = sdk.register_lens_sensor_active_mode(SENSOR_CODE, SENSOR_RUN, _mode())
        spec = _spec(mode["id"])
        roi = spec["registered_roi"]
        roi["lens_revision_id"] = "6c48e967-74b4-5901-8186-ba6fafe4797b"
        roi["sensor_roi_bounds_row_col_exclusive"] = [1020, 1028, 7000, 7008]
        roi["analytic_scene"].update(
            shape_row_col=[4096, 4096], horizontal_fov_deg=1.0, edge_offset_deg=0.0
        )
        spec["sampling_goal"]["optical_tile_estimated_working_budget_bytes"] = 2 * 1024**3
        from dataclasses import replace

        from camera_platform.lens_sensor_frequency_sampling import plan_frequency_sampling
        from camera_platform.lens_sensor_out_of_core_raw_job import _ANGLES_DEG, _resource_policy
        from camera_platform.lens_sensor_registered_roi_disk_raw_job import _execution_inputs
        from camera_platform.lens_sensor_registered_roi_sampling_adapter import (
            frequency_sampling_kwargs_for_registered_roi,
        )
        from camera_platform.lens_sensor_scene_geometry_tile import (
            make_analytic_strict_geometry_provider,
        )
        from camera_platform.lens_sensor_streamed_offaxis_mean import (
            preflight_streamed_analytic_offaxis_mean,
        )

        active, binding, _lens, _inspected, source, sensor, *_ = _execution_inputs(
            app.state.service, roi
        )
        kwargs = frequency_sampling_kwargs_for_registered_roi(
            roi, optics=source.optics, sensor=sensor,
            active_mode={**active, "id": binding["declaration_id"]},
            scene_grid_mode="adaptive",
        )
        plan = plan_frequency_sampling(**kwargs)
        assert [step["scene_shape_row_col"] for step in plan["fixed_fov_refinement_grids"]] == [
            [504, 504], [1008, 1008], [2016, 2016]
        ]
        provider = make_analytic_strict_geometry_provider(
            source.optics, scene_shape_row_col=(2016, 2016),
            scene_horizontal_fov_deg=1.0, wavelengths_nm=source.wavelengths_nm,
            bright_side_photons=source.bright_photons,
            analytic_slanted_edge_offset_deg=0.0,
            source_rasterization="cell_area_integrated",
        )
        policy = replace(
            _resource_policy().optical_tile,
            estimated_working_budget_bytes=2 * 1024**3,
        )
        preflight = preflight_streamed_analytic_offaxis_mean(
            provider, sensor,
            sensor_tile_bounds_row_col_exclusive=(1020, 1028, 7000, 7008),
            source_tile_shape_row_col=(64, 64), sample_angles_deg=_ANGLES_DEG,
            resource_policy=policy,
        )
        assert preflight["status"] == "ready_under_declared_policy"
        assert 1024**3 < preflight["combined_policy_estimate_bytes"] < 2 * 1024**3


@pytest.mark.skipif(
    os.environ.get("CAMERA_TEST_FULL_FREQUENCY_RETRACE") != "1",
    reason="Run explicitly with pinned Lens/Sensor DB and extended numerical work",
)
def test_explicit_psf_pitch_mismatch_still_runs_source_node_diagnostic(tmp_path: Path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned local Lens/Sensor DB unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = transport
        mode = sdk.register_lens_sensor_active_mode(SENSOR_CODE, SENSOR_RUN, _mode())
        spec = _spec(mode["id"])
        roi = spec["registered_roi"]
        roi["lens_revision_id"] = "6c48e967-74b4-5901-8186-ba6fafe4797b"
        roi["sensor_roi_bounds_row_col_exclusive"] = [1020, 1028, 7000, 7008]
        roi["analytic_scene"].update(
            shape_row_col=[2048, 2048], horizontal_fov_deg=2.0, edge_offset_deg=0.0
        )
        goal = spec["sampling_goal"]
        goal["mode"] = "explicit"
        goal["max_refinements"] = 0
        goal["psf_resolution_mode"] = "pinned_same_ray_retrace_diagnostic"
        goal["psf_target_pitch_um"] = 0.55
        goal["optical_tile_estimated_working_budget_bytes"] = 1024**3
        submitted = sdk.submit_lens_sensor_frequency_target(spec)
        from camera_platform.worker import work

        work(app.state.service, once=True, timeout=180)
        completed = sdk.lens_sensor_frequency_target(submitted["id"])
        assert completed["status"] == "succeeded", completed["error"]
        summary = completed["result"]["summary"]
        assert summary["execution"]["steps"][0]["mean_electrons_sha256_by_phase"]
        diagnostic = summary["psf_target_band_diagnostic"]
        assert diagnostic["refined_histogram"]["pitch_um_row_col"] == [0.55, 0.55]
        assert diagnostic["stored_histogram"]["dc_normalized_complex_otf"]
        assert diagnostic["fixed_grid_camera_sensitivity"]["status"] == (
            "inconclusive_psf_pitch_differs_from_executed_oi_grid"
        )
        assert diagnostic["fixed_grid_camera_sensitivity"][
            "same_grid_camera_comparison_executed"
        ] is False
