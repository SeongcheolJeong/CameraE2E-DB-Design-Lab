"""Registered active-mode ROI RAW contracts, distinct from the virtual crop."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.lens_sensor_fullfield_study_preflight import MODE_SCHEMA
from camera_platform.lens_sensor_registered_roi_disk_raw_job import (
    JOB_KIND,
    PENDING_SUBMISSION_SCHEMA,
    SCHEMA,
    WORKER_TIMEOUT_S,
    _registered_mode,
    _roi_tiles,
    default_spec,
    specification,
)
from camera_platform.registry import IntegrityError, digest
from camera_platform.sdk import PlatformClient
from camera_platform.sensor_db_bridge import inspect_sensor_revision
from camera_platform.service import PlatformService

WORKSPACE = Path(__file__).resolve().parents[2]
LENS_REVISION = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"
SENSOR_CODE = "DEP-2209-801"
SENSOR_RUN = "74e15e696e8d14827bc641346dc92d113a24997da6ec13efb51d9e2583a4f188"


def _spec() -> dict:
    spec = default_spec()
    spec["lens_revision_id"] = LENS_REVISION
    spec["sensor_revision"] = {"identifier": SENSOR_CODE, "run_id": SENSOR_RUN}
    spec["active_mode_declaration_id"] = "active_mode_declaration-" + "a" * 32
    spec["sensor_roi_bounds_row_col_exclusive"] = [1000, 1008, 12000, 12008]
    spec["sensor_tile_shape_row_col"] = [4, 8]
    spec["readout_row_chunk_rows"] = 4
    spec["analytic_scene"].update(
        shape_row_col=[4096, 28000], horizontal_fov_deg=42.0, edge_offset_deg=14.0
    )
    spec["accept_approximate_lens_radiometry"] = True
    spec["acknowledge_unverified_mode_and_uniform_sensor_response"] = True
    return spec


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


def test_spec_keeps_long_virtual_scene_grid_separate_from_small_output_roi():
    spec = _spec()
    spec["analytic_scene"]["shape_row_col"] = [8192, 56000]
    assert specification(spec)["analytic_scene"]["shape_row_col"] == [8192, 56000]
    assert _roi_tiles([1000, 1008, 12000, 12008], [4, 8]) == (
        (1000, 1004, 12000, 12008),
        (1004, 1008, 12000, 12008),
    )
    invalid = deepcopy(spec)
    invalid["active_mode_declaration_id"] = "/private/tmp/mode.json"
    with pytest.raises(ValueError, match="never a path"):
        specification(invalid)
    invalid = deepcopy(spec)
    invalid["readout_row_chunk_rows"] = 8
    with pytest.raises(ValueError, match="smaller than ROI rows"):
        specification(invalid)
    invalid = deepcopy(spec)
    invalid["analytic_scene"]["shape_row_col"] = [8193, 65536]
    with pytest.raises(ValueError, match="virtual-grid cell policy"):
        specification(invalid)


def test_registered_mode_can_block_full_boundary_but_approve_supported_roi_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from camera_platform import lens_sensor_registered_roi_disk_raw_job as job_module

    spec = _spec()
    checked = {
        "status": "blocked",
        "reason": "active_boundary_exceeds_saved_lens_field_at_one_or_more_wavelengths",
        "active_mode": {
            **{key: value for key, value in _mode().items()},
        },
        "source_validation": {
            "file_hash_verified": True,
            "declaration_values_match": True,
            "physical_mode_verified": False,
        },
        "registered_declaration": {
            "id": spec["active_mode_declaration_id"],
            "artifact_sha256": "f" * 64,
            "source_kind": "engineering_declaration",
            "physical_mode_verified": False,
        },
        "geometric_fov": {
            "status": "outside_saved_model_support",
            "reason": "active_boundary_exceeds_saved_lens_field_at_one_or_more_wavelengths",
        },
    }
    monkeypatch.setattr(
        job_module, "preflight_registered_fullfield_study", lambda *args, **kwargs: checked
    )
    service = SimpleNamespace(registry=object(), workspace=tmp_path)
    _, binding = _registered_mode(service, spec)
    assert binding["full_boundary_saved_field_status"] == "outside_saved_model_support"
    assert binding["global_sensor_roi_bounds_row_col_exclusive"] == [1000, 1008, 12000, 12008]
    assert binding["physical_mode_verified"] is False
    for change in (
        {"sensor_center_offset_from_optical_axis_um_row_col": [1.0, 0.0]},
        {"sensor_orientation_deg_clockwise": 90},
        {"cfa_origin_row_col": [1, 0]},
        {"image_plane_binding": {
            "reference_plane": "saved_prescription_image_plane", "axial_offset_um": 1.0
        }},
    ):
        checked["active_mode"] = {**_mode(), **change}
        with pytest.raises(ValueError, match="zero optical-axis offset"):
            _registered_mode(service, spec)
    checked["active_mode"] = _mode()
    checked["source_validation"]["file_hash_verified"] = False
    with pytest.raises(ValueError, match="unusable"):
        _registered_mode(service, spec)


def test_real_db_registered_long_mode_async_submit_and_worker_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned local Lens and Sensor DB unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = transport
        option = sdk.lens_sensor_disk_raw_sensor_option(SENSOR_CODE, SENSOR_RUN)
        assert option["supported_disk_raw_diagnostic"] is True
        assert option["sensor_revision"]["result_sha256"] == _mode()["sensor_result_sha256"]
        assert option["pixel_pitch_um"] == 1.22
        mode = sdk.register_lens_sensor_active_mode(SENSOR_CODE, SENSOR_RUN, _mode())
        field = sdk.inspect_registered_lens_sensor_fov(
            LENS_REVISION, SENSOR_CODE, SENSOR_RUN, mode["id"]
        )["geometric_fov"]
        assert field["status"] == "within_saved_model_support"
        assert field["reason"] is None
        template = sdk.lens_sensor_registered_roi_disk_raw_template()
        assert template["maximum_active_axis_coordinate_policy"] >= 14000
        assert template["resource_policy_is_hardware_limit"] is False
        spec = _spec()
        spec["active_mode_declaration_id"] = mode["id"]
        from camera_platform import lens_sensor_registered_roi_disk_raw_job as job_module
        from pyisetcam import lens_patents

        def forbidden(*_args, **_kwargs):
            raise AssertionError("Submission must not load PSF or plan optical tiles")

        with monkeypatch.context() as guard:
            guard.setattr(job_module, "_registered_mode", forbidden)
            guard.setattr(job_module, "_execution_inputs", forbidden)
            guard.setattr(job_module, "preflight_streamed_analytic_offaxis_mean", forbidden)
            guard.setattr(job_module, "preflight_registered_fullfield_study", forbidden)
            guard.setattr(lens_patents, "lens_patent_raytrace_optics", forbidden)
            submitted = sdk.submit_lens_sensor_registered_roi_disk_raw(spec)
        assert submitted["job_kind"] == JOB_KIND
        assert submitted["submission_contract_schema"] == PENDING_SUBMISSION_SCHEMA
        assert submitted["submission_preflight_status"] == "worker_preflight_pending"
        assert submitted["registered_mode_binding"]["active_shape_row_col"] == [2048, 14000]
        assert submitted["registered_mode_binding"]["physical_mode_verified"] is False
        assert submitted["cheap_work_admission"]["status"] == "ready_for_model_dependent_preflight"
        assert (
            submitted["cheap_work_admission"][
                "model_dependent_psf_halo_and_distortion_checked"
            ] is False
        )
        assert "resource_preflight" not in submitted
        assert sdk.lens_sensor_registered_roi_disk_raw(submitted["id"])["status"] == "queued"
        repeated = sdk.submit_lens_sensor_registered_roi_disk_raw(spec)
        assert repeated["id"] == submitted["id"] and repeated["cache_hit"] is True
        calls = {}

        def fake_wait(identity, *, timeout, poll_interval, expected_kind):
            calls.update(
                identity=identity, timeout=timeout, poll_interval=poll_interval,
                expected_kind=expected_kind,
            )
            return submitted

        sdk.wait = fake_wait
        assert sdk.wait_lens_sensor_registered_roi_disk_raw(submitted["id"]) == submitted
        assert calls == {
            "identity": submitted["id"], "timeout": 1860, "poll_interval": 0.5,
            "expected_kind": JOB_KIND,
        }
        assert WORKER_TIMEOUT_S == 1800
        from camera_platform.worker import work

        work(app.state.service, once=True, timeout=120)
        completed = sdk.lens_sensor_registered_roi_disk_raw(submitted["id"])
        assert completed["status"] == "succeeded", completed["error"]
        summary = completed["result"]["summary"]
        assert summary["frozen_submission_sha256"] == submitted["content_hash"]
        assert summary["resource_preflight"]["status"] == "ready_under_declared_policy"
        assert summary["resource_preflight"]["model_dependent_total_work"]["status"] == (
            "ready_under_declared_total_work_policy"
        )
        assert summary["execution"]["output_roi_shape_row_col"] == [8, 8]
        assert summary["physical_fov_qualified"] is False
        assert summary["product_full_frame_qualified"] is False
        for section, key in (
            ("lens", "revision_sha256"),
            ("lens", "source_db_sha256"),
            ("sensor", "config_sha256"),
        ):
            altered = deepcopy(completed)
            altered["result"]["summary"]["source_identities"][section][key] = "0" * 64
            with monkeypatch.context() as guard:
                guard.setattr(
                    app.state.service.registry, "get_run",
                    lambda _id, saved=altered: saved,
                )
                with pytest.raises(IntegrityError, match="pinned sources"):
                    job_module.get(app.state.service, submitted["id"])


def test_real_db_4x_request_queues_then_worker_records_numeric_policy_failure(tmp_path: Path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned local Lens and Sensor DB unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = transport
        mode = sdk.register_lens_sensor_active_mode(SENSOR_CODE, SENSOR_RUN, _mode())
        spec = _spec()
        spec["active_mode_declaration_id"] = mode["id"]
        spec["analytic_scene"]["shape_row_col"] = [8192, 56000]
        submitted = sdk.submit_lens_sensor_registered_roi_disk_raw(spec)
        assert submitted["status"] == "queued"
        assert submitted["submission_preflight_status"] == "worker_preflight_pending"
        from camera_platform.worker import work

        work(app.state.service, once=True, timeout=120)
        failed = sdk.lens_sensor_registered_roi_disk_raw(submitted["id"])
        assert failed["status"] == "failed"
        evidence = failed["result"]["preflight_failure"]
        assert evidence["stage"] == "model_dependent_preflight"
        assert evidence["cause_status"] == "blocked_working_memory_policy"
        budget = evidence["resource_evidence"]["estimated_working_budget_bytes"]
        estimate = evidence["resource_evidence"]["combined_policy_estimate_bytes"]
        assert estimate > budget > 0
        assert evidence["optical_convolution_executed"] is False
        assert failed["artifacts"] == []


def test_legacy_queued_request_fails_closed_after_source_upgrade(tmp_path: Path):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    legacy = {
        "name": "legacy queued ROI",
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": _spec(),
        "environment": {"platform_version": "older_source"},
    }
    queued = service.registry.submit(legacy, digest(legacy))
    from camera_platform.worker import work

    work(service, once=True, timeout=30)
    from camera_platform.lens_sensor_registered_roi_disk_raw_job import get

    failed = get(service, queued["id"])
    assert failed["status"] == "failed"
    evidence = failed["result"]["preflight_failure"]
    assert evidence["reason_code"] == "legacy_environment_unavailable_resubmit"
    assert evidence["optical_convolution_executed"] is False
