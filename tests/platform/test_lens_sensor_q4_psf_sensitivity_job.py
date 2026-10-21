"""q4 fixed-grid sensitivity pins an owner-bound candidate and one ROI grid."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.lens_sensor_q4_psf_sensitivity_job import (
    ARTIFACT_NAME,
    COMPARISON_SCHEMA,
    EXTENDED_SPARSE_ROI_COORDINATE_POLICY,
    JOB_KIND,
    SCHEMA,
    _candidate_binding,
    _prepare_pending,
    compare_fixed_grid_q4_geometric_psf,
    default_spec,
    execute,
    get,
    specification,
)
from camera_platform.registry import IntegrityError, SQLiteRegistry, digest
from camera_platform.sdk import PlatformClient
from pyisetcam.types import Sensor


def _spec():
    spec = default_spec()
    roi = spec["registered_roi"]
    roi["lens_revision_id"] = "6c48e967-74b4-5901-8186-ba6fafe4797b"
    roi["sensor_revision"] = {"identifier": "DEP-2209-801", "run_id": "a" * 64}
    roi["active_mode_declaration_id"] = "active_mode_declaration-" + "b" * 32
    roi["accept_approximate_lens_radiometry"] = True
    roi["acknowledge_unverified_mode_and_uniform_sensor_response"] = True
    spec["candidate_run_id"] = "run-" + "c" * 32
    spec["candidate_asset_sha256"] = "d" * 64
    return spec


def _candidate(spec):
    return {
        "candidate_run_id": spec["candidate_run_id"],
        "candidate_run_content_hash": "hash",
        "candidate_asset_sha256": spec["candidate_asset_sha256"],
        "candidate_manifest_artifact_sha256": "0" * 64,
        "candidate_psf_array_sha256": "e" * 64,
        "parent_candidate_run_id": "run-" + "9" * 32,
        "parent_candidate_asset_sha256": "8" * 64,
        "source_psf_asset_sha256": "f" * 64,
        "lens_db_sha256": "1" * 64,
        "lens_revision_id": spec["registered_roi"]["lens_revision_id"],
        "lens_revision_content_sha256": "2" * 64,
        "source_prescription_sha256": "3" * 64,
        "q4_histogram_pitch_mm_row_col": [0.0001, 0.0001],
        "q4_psf_shape_row_col_field_wave": [2045, 2045, 5, 3],
        "histogram_gap_within_declared_tolerance_model_only": True,
        "estimated_working_array_bytes_not_peak_rss": 350 * 1024**2,
    }


def _baseline():
    return {
        "pinned_source_binding": {
            "geometric_psf_sha256": "f" * 64,
            "lens_revision_sha256": "2" * 64,
            "lens_source_db_sha256": "1" * 64,
        },
        "registered_mode_binding": {"declaration_id": "mode-1"},
        "environment": {"build": "unit"},
    }


def test_explicit_extended_one_grid_and_policy_limit():
    spec = _spec()
    assert specification(spec)["fixed_grid_goal"]["scene_shape_row_col"] == [2048, 2048]
    spec["registered_roi"]["analytic_scene"]["shape_row_col"] = [2048, 16000]
    spec["fixed_grid_goal"]["scene_shape_row_col"] = [16384, 128000]
    spec["fixed_grid_goal"]["coordinate_policy"] = (
        EXTENDED_SPARSE_ROI_COORDINATE_POLICY
    )
    spec["fixed_grid_goal"]["optical_tile_estimated_working_budget_bytes"] = 8 * 1024**3
    checked = specification(spec)
    assert checked["registered_roi"]["analytic_scene"]["shape_row_col"] == [2048, 16000]
    assert checked["fixed_grid_goal"]["scene_shape_row_col"] == [16384, 128000]
    normal = deepcopy(spec)
    normal["fixed_grid_goal"]["coordinate_policy"] = "registered_roi_v1"
    with pytest.raises(ValueError, match="coordinate policy"):
        specification(normal)
    too_large = deepcopy(spec)
    too_large["fixed_grid_goal"]["scene_shape_row_col"] = [32768, 131072]
    with pytest.raises(ValueError, match="coordinate policy"):
        specification(too_large)


def test_candidate_and_noise_contract_is_exact():
    invalid = _spec()
    invalid["registered_roi"]["capture"]["noise_flag"] = 2
    with pytest.raises(ValueError, match="noise_flag=0"):
        specification(invalid)
    invalid = _spec()
    invalid["candidate_asset_sha256"] = "d" * 63
    with pytest.raises(ValueError, match="SHA-256"):
        specification(invalid)
    invalid = _spec()
    invalid["fixed_grid_goal"]["target_fractions_native_nyquist"] = [0.9, 1.0]
    with pytest.raises(ValueError, match="below native Nyquist"):
        specification(invalid)
    invalid = _spec()
    invalid["candidate_asset_path"] = "/private/tmp/unbound.npz"
    with pytest.raises(ValueError, match="exact"):
        specification(invalid)


def test_sdk_template_route_is_available_without_a_candidate(tmp_path: Path):
    workspace = Path(__file__).resolve().parents[2]
    app = create_app(
        tmp_path / "state", workspace, start_worker=False, seed_demo=False
    )
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get(
            "/api/v1/session"
        ).json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = transport
        template = sdk.lens_sensor_q4_psf_sensitivity_template()
        assert template["specification"]["schema"] == SCHEMA
        assert template["specification"]["fixed_grid_goal"]["coordinate_policy"] == (
            "registered_roi_v1"
        )


def test_candidate_binding_requires_completed_q4_owner_and_revision(monkeypatch):
    from camera_platform import lens_dense_geometric_psf_q4_rebin_job as q4_module

    spec = _spec()
    candidate = _candidate(spec)
    manifest = {
        "candidate_asset_sha256": candidate["candidate_asset_sha256"],
        "candidate_psf_array_sha256": candidate["candidate_psf_array_sha256"],
        "lens_revision_id": candidate["lens_revision_id"],
        "source_psf_asset_sha256": candidate["source_psf_asset_sha256"],
        "lens_db_sha256": candidate["lens_db_sha256"],
        "lens_revision_content_sha256": candidate["lens_revision_content_sha256"],
        "source_prescription_sha256": candidate["source_prescription_sha256"],
        "candidate_histogram_pitch_mm_row_col": candidate["q4_histogram_pitch_mm_row_col"],
        "candidate_psf_shape_row_col_field_wave": (
            candidate["q4_psf_shape_row_col_field_wave"]
        ),
        "histogram_gap_within_declared_tolerance_model_only": True,
        "histogram_target_status": "numeric_gap_within_declared_tolerance_model_only",
        "estimated_working_array_bytes_not_peak_rss": (
            candidate["estimated_working_array_bytes_not_peak_rss"]
        ),
        "optical_psf_semantics": "geometric_pre_pixel_ray_histogram",
        "pixel_aperture_included": False,
        "diffraction_included": False,
        "histogram_refinement_factor": 4,
    }
    record = {
        "id": candidate["candidate_run_id"], "content_hash": "hash", "status": "succeeded",
        "result": {"summary": {
            "candidate_manifest": manifest,
            "candidate_archive_artifact": {"sha256": candidate["candidate_asset_sha256"]},
            "candidate_manifest_artifact": {"sha256": "0" * 64},
            "pinned_parent_and_lens_binding": {
                "parent_candidate_run_id": candidate["parent_candidate_run_id"],
                "parent_candidate_asset_sha256": candidate["parent_candidate_asset_sha256"],
            },
        }},
    }
    monkeypatch.setattr(q4_module, "get", lambda *_args: record)
    assert _candidate_binding(object(), spec) == candidate
    record["status"] = "running"
    with pytest.raises(ValueError, match="must have succeeded"):
        _candidate_binding(object(), spec)
    record["status"] = "succeeded"
    manifest["lens_revision_id"] = "0" * 36
    with pytest.raises(ValueError, match="does not match"):
        _candidate_binding(object(), spec)
    manifest["lens_revision_id"] = candidate["lens_revision_id"]
    manifest["histogram_target_status"] = "inconclusive_target_histogram_gap"
    with pytest.raises(ValueError, match="does not match"):
        _candidate_binding(object(), spec)


def test_pending_rejects_wrong_source_and_under_budget(monkeypatch):
    from camera_platform import lens_sensor_q4_psf_sensitivity_job as module
    from camera_platform import lens_sensor_registered_roi_disk_raw_job as roi_module

    spec = _spec()
    candidate = _candidate(spec)
    monkeypatch.setattr(roi_module, "_prepare_pending", lambda *_args: _baseline())
    monkeypatch.setattr(module, "_candidate_binding", lambda *_args: candidate)
    frozen = _prepare_pending(object(), spec)
    assert frozen["candidate_binding"] == candidate
    assert frozen["default_worker_timeout_seconds"] == 1800
    extended = deepcopy(spec)
    extended["fixed_grid_goal"]["coordinate_policy"] = (
        EXTENDED_SPARSE_ROI_COORDINATE_POLICY
    )
    assert _prepare_pending(object(), extended)["default_worker_timeout_seconds"] == 3600
    small = deepcopy(spec)
    small["fixed_grid_goal"]["optical_tile_estimated_working_budget_bytes"] = 256 * 1024**2
    with pytest.raises(ValueError, match="memory policy"):
        _prepare_pending(object(), small)
    bad = dict(candidate, source_psf_asset_sha256="4" * 64)
    monkeypatch.setattr(module, "_candidate_binding", lambda *_args: bad)
    with pytest.raises(ValueError, match="registered Lens"):
        _prepare_pending(object(), spec)


def test_pair_reports_mean_phase_and_rejects_changed_physical_input(monkeypatch):
    from camera_platform import lens_sensor_q4_psf_overlay as overlay_module
    from camera_platform import lens_sensor_registered_roi_frequency_execution as runner_module
    from camera_platform.lens_sensor_streamed_full_raw import _sensor_identity

    source = {"raytrace": {"asset_sha256": "source"}}
    candidate = {"raytrace": {"asset_sha256": "candidate"}}
    sensor = Sensor(fields={"stable": True})
    monkeypatch.setattr(
        overlay_module, "optics_with_q4_geometric_psf_candidate",
        lambda *_args: candidate,
    )
    same_source_identity = ["same-phase-0", "same-phase-1"]
    changed = {"different": False}

    def fake_run(optics, _sensor, **kwargs):
        assert kwargs["max_refinements"] == 0
        assert kwargs["return_transient_means"] is True
        shape = list(kwargs["first_scene_shape_row_col"])
        is_candidate = optics is candidate
        physical = {"fov_deg": 30.0, "optics_state_sha256": "different"}
        if is_candidate and changed["different"]:
            physical["fov_deg"] = 31.0
        capture = {
            "status": "inconclusive_single_grid_no_refinement",
            "sensor_identity": _sensor_identity(sensor),
            "fixed_physical_input": physical,
            "steps": [{
                "scene_shape_row_col": shape,
                "oi_pitch_um": 0.147281899,
                "source_identity_by_phase": same_source_identity,
                "mean_electrons_sha256_by_phase": ["phase0", "phase1"],
                "mean_phase_signal_valid": True,
            }],
        }
        offset = 0.01 if is_candidate else 0.0
        return capture, (
            np.full((8, 8), 1.0 + offset),
            np.full((8, 8), 2.0 + offset),
        )

    monkeypatch.setattr(runner_module, "run_registered_roi_frequency_sampling", fake_run)
    report = compare_fixed_grid_q4_geometric_psf(
        source, sensor,
        {"source_psf_asset_sha256": "f" * 64, "candidate_asset_sha256": "d" * 64},
        {}, runner_kwargs={
            "first_scene_shape_row_col": (16384, 128000),
            "max_refinements": 0,
            "coordinate_policy": EXTENDED_SPARSE_ROI_COORDINATE_POLICY,
        },
    )
    assert report["schema"] == COMPARISON_SCHEMA
    assert report["status"] == "numeric_q4_geometric_psf_fixed_grid_sensitivity_model_only"
    assert report["same_scene_and_sensor_inputs"] is True
    assert report["mean_electron_full_scale_normalized_max_delta"] == pytest.approx(
        0.01 / 2.01
    )
    assert report["shift_response_full_scale_normalized_max_delta"] == pytest.approx(
        0.0, abs=1e-14
    )
    assert report["histogram_pitch_only_effect_isolated"] is False
    changed["different"] = True
    with pytest.raises(IntegrityError, match="identical Scene/Sensor"):
        compare_fixed_grid_q4_geometric_psf(
            source, sensor,
            {"source_psf_asset_sha256": "f" * 64, "candidate_asset_sha256": "d" * 64},
            {}, runner_kwargs={
                "first_scene_shape_row_col": (16384, 128000),
                "max_refinements": 0,
                "coordinate_policy": EXTENDED_SPARSE_ROI_COORDINATE_POLICY,
            },
        )


def test_execute_preserves_extended_one_grid_and_owner_bound_result(monkeypatch, tmp_path: Path):
    from camera_platform import lens_dense_geometric_psf_q4_rebin_job as q4_module
    from camera_platform import lens_sensor_q4_psf_sensitivity_job as module
    from camera_platform import lens_sensor_registered_roi_disk_raw_job as roi_module
    from camera_platform import lens_sensor_registered_roi_sampling_adapter as adapter_module

    spec = specification(_spec())
    spec["fixed_grid_goal"]["coordinate_policy"] = (
        EXTENDED_SPARSE_ROI_COORDINATE_POLICY
    )
    spec["fixed_grid_goal"]["scene_shape_row_col"] = [16384, 128000]
    spec["fixed_grid_goal"]["optical_tile_estimated_working_budget_bytes"] = 8 * 1024**3
    candidate = _candidate(spec)
    baseline = _baseline()
    optics = {"raytrace": {"asset_sha256": "source"}}
    sensor = Sensor(fields={"stable": True})
    provider = SimpleNamespace(
        optics=optics,
        wavelengths_nm=np.array([550.0]),
        bright_photons=np.array([1.0]),
    )
    mode = {"pixel_pitch_um": 1.22}
    binding = {"declaration_id": "mode-1"}
    lens = {"identity": {
        "prescription_sha256": candidate["source_prescription_sha256"],
        "source_db_sha256": candidate["lens_db_sha256"],
    }}
    inspected = {"revision": {"id": "sensor-1"}}
    frozen = {
        "name": spec["name"], "job_kind": JOB_KIND, "plugin_id": SCHEMA,
        "specification": spec, "registered_roi_submission": baseline,
        "candidate_binding": candidate, "environment": baseline["environment"],
        "default_worker_timeout_seconds": 3600,
        "submission_preflight_status": (
            "owner_bound_q4_candidate_and_registered_mode_worker_preflight_pending"
        ),
        "limitation": module.LIMITATION,
    }
    registry = SQLiteRegistry(tmp_path / "state")
    service = SimpleNamespace(registry=registry, loaded_environment=baseline["environment"])
    queued = registry.submit(frozen, digest(frozen))
    claimed = registry.claim()
    assert claimed["id"] == queued["id"]
    monkeypatch.setattr(module, "_prepare_pending", lambda *_args: frozen)
    monkeypatch.setattr(module, "_candidate_binding", lambda *_args: candidate)
    monkeypatch.setattr(roi_module, "_prepare_pending", lambda *_args: baseline)
    monkeypatch.setattr(roi_module, "_model_identities_match_pins", lambda *_args: True)
    monkeypatch.setattr(roi_module, "_recheck_registered_blob", lambda *_args: None)
    monkeypatch.setattr(roi_module, "_execution_inputs", lambda *_args: (
        mode, binding, lens, inspected, provider, sensor, None, None,
    ))
    loaded = {
        "candidate_psf_array_sha256": candidate["candidate_psf_array_sha256"],
        "source_prescription_sha256": candidate["source_prescription_sha256"],
        "candidate_histogram_pitch_mm_row_col": (
            candidate["q4_histogram_pitch_mm_row_col"]
        ),
        "candidate_psf_shape_row_col_field_wave": (
            candidate["q4_psf_shape_row_col_field_wave"]
        ),
    }
    expected_loader = {}

    def fake_loader(_service, _run_id, **kwargs):
        expected_loader.update(kwargs)
        return loaded, {"psf_function": np.array([1.0])}

    monkeypatch.setattr(q4_module, "load_verified_rebin", fake_loader)
    monkeypatch.setattr(
        adapter_module, "frequency_sampling_kwargs_for_registered_roi",
        lambda *_args, **_kwargs: {
            "native_pixel_pitch_um_row_col": (1.22, 1.22),
            "image_distance_m": 0.014,
        },
    )
    runner = {}

    def fake_compare(_source, _sensor, _manifest, _arrays, *, runner_kwargs):
        runner.update(runner_kwargs)
        return {
            "schema": COMPARISON_SCHEMA,
            "status": "inconclusive_edge_signal_support",
            "scene_shape_row_col": [16384, 128000],
            "same_scene_and_sensor_inputs": True,
            "histogram_pitch_only_effect_isolated": False,
            "scene_sampling_convergence_established": False,
            "pupil_density_convergence_qualified_all_nodes": False,
            "diffraction_included": False,
            "readout_or_isp_executed": False,
            "physical_mtf_comparison_allowed": False,
            "physical_accuracy_qualified": False,
            "product_ranking_allowed": False,
        }

    monkeypatch.setattr(module, "compare_fixed_grid_q4_geometric_psf", fake_compare)
    original_blob_path = registry.blob_path
    monkeypatch.setattr(registry, "blob_path", lambda _sha: tmp_path)
    result = execute(service, claimed)
    assert runner["first_scene_shape_row_col"] == (16384, 128000)
    assert runner["max_refinements"] == 0
    assert runner["coordinate_policy"] == EXTENDED_SPARSE_ROI_COORDINATE_POLICY
    assert runner["max_scene_axis"] == 131072
    assert runner["max_virtual_scene_cells"] == 2_147_483_648
    assert runner["resource_policy"].estimated_working_budget_bytes == 8 * 1024**3
    assert expected_loader["expected_parent_run_id"] == candidate["parent_candidate_run_id"]
    assert expected_loader["expected_parent_asset_sha256"] == (
        candidate["parent_candidate_asset_sha256"]
    )
    assert result["artifacts"][0]["name"] == ARTIFACT_NAME
    assert result["summary"]["policy_is_hardware_limit"] is False
    monkeypatch.setattr(registry, "blob_path", original_blob_path)
    registry.finish(claimed["id"], result=result)
    assert get(service, claimed["id"])["result"]["summary"] == result["summary"]
    monkeypatch.setattr(
        registry, "artifact_binding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            IntegrityError("missing owner binding")
        ),
    )
    with pytest.raises(IntegrityError, match="owner binding"):
        get(service, claimed["id"])
