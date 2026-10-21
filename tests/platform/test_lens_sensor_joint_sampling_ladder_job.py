"""Durable joint Scene/PSF study pins a candidate and reports bounded evidence."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.lens_sensor_joint_sampling_ladder_job import (
    ARTIFACT_NAME,
    DEFAULT_COORDINATE_POLICY,
    EXTENDED_SPARSE_ROI_COORDINATE_POLICY,
    JOB_KIND,
    SCHEMA,
    _prepare_pending,
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
        "candidate_asset_sha256": spec["candidate_asset_sha256"],
        "candidate_psf_array_sha256": "e" * 64,
        "source_psf_asset_sha256": "f" * 64,
        "lens_db_sha256": "1" * 64,
        "lens_revision_content_sha256": "2" * 64,
        "source_prescription_sha256": "3" * 64,
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


def test_default_contract_and_explicit_extended_four_grid():
    checked = specification(_spec())
    assert checked["schema"] == SCHEMA
    assert checked["joint_goal"]["coordinate_policy"] == DEFAULT_COORDINATE_POLICY
    assert checked["joint_goal"]["scene_grid_ladder_row_col"] == [
        [2048, 2048], [4096, 4096], [8192, 8192],
    ]
    extended = deepcopy(_spec())
    extended["registered_roi"]["analytic_scene"]["shape_row_col"] = [2048, 16000]
    extended["joint_goal"]["scene_grid_ladder_row_col"] = [
        [2048, 16000], [4096, 32000], [8192, 64000], [16384, 128000],
    ]
    extended["joint_goal"]["coordinate_policy"] = EXTENDED_SPARSE_ROI_COORDINATE_POLICY
    assert specification(extended)["joint_goal"]["scene_grid_ladder_row_col"][-1] == [
        16384, 128000,
    ]
    normal = deepcopy(extended)
    normal["joint_goal"]["coordinate_policy"] = DEFAULT_COORDINATE_POLICY
    with pytest.raises(ValueError, match="coordinate policy"):
        specification(normal)


def test_sdk_template_route(tmp_path: Path):
    app = create_app(tmp_path / "state", Path(__file__).resolve().parents[2],
                     start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get(
            "/api/v1/session"
        ).json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = transport
        template = sdk.lens_sensor_joint_sampling_ladder_template()
        assert template["specification"]["schema"] == SCHEMA
        assert template["specification"]["joint_goal"]["coordinate_policy"] == (
            DEFAULT_COORDINATE_POLICY
        )


def test_ladder_and_candidate_inputs_are_exact_and_bounded():
    invalid = deepcopy(_spec())
    invalid["joint_goal"]["scene_grid_ladder_row_col"][1] = [4096, 4098]
    with pytest.raises(ValueError, match="double both axes"):
        specification(invalid)
    invalid = deepcopy(_spec())
    invalid["joint_goal"]["scene_grid_ladder_row_col"][0] = [1024, 1024]
    with pytest.raises(ValueError, match="First Scene grid"):
        specification(invalid)
    invalid = deepcopy(_spec())
    invalid["joint_goal"]["coordinate_policy"] = "unbounded"
    with pytest.raises(ValueError, match="coordinate policy"):
        specification(invalid)
    invalid = deepcopy(_spec())
    invalid["candidate_asset_sha256"] = "d" * 63
    with pytest.raises(ValueError, match="SHA-256"):
        specification(invalid)
    invalid = deepcopy(_spec())
    invalid["registered_roi"]["capture"]["noise_flag"] = 2
    with pytest.raises(ValueError, match="noise_flag=0"):
        specification(invalid)
    invalid = deepcopy(_spec())
    invalid["candidate_asset_path"] = "/tmp/unbound.npz"
    with pytest.raises(ValueError, match="exact"):
        specification(invalid)


def test_pending_request_binds_candidate_to_registered_psf(monkeypatch):
    from camera_platform import lens_sensor_joint_sampling_ladder_job as job_module
    from camera_platform import lens_sensor_registered_roi_disk_raw_job as roi_module

    spec = _spec()
    monkeypatch.setattr(roi_module, "_prepare_pending", lambda _service, _roi: _baseline())
    monkeypatch.setattr(job_module, "_candidate_binding", lambda _service, _spec: _candidate(spec))
    frozen = _prepare_pending(object(), spec)
    assert frozen["job_kind"] == JOB_KIND
    assert frozen["candidate_binding"]["source_psf_asset_sha256"] == "f" * 64
    assert frozen["default_worker_timeout_seconds"] == 1800
    bad = _candidate(spec)
    bad["source_psf_asset_sha256"] = "0" * 64
    monkeypatch.setattr(job_module, "_candidate_binding", lambda _service, _spec: bad)
    with pytest.raises(ValueError, match="differs from registered Lens"):
        _prepare_pending(object(), spec)


def test_execute_and_owner_bound_result_without_optical_compute(monkeypatch, tmp_path: Path):
    from camera_platform import lens_dense_geometric_psf_candidate_job as candidate_module
    from camera_platform import lens_sensor_dense_psf_sensitivity as sensitivity_module
    from camera_platform import lens_sensor_joint_sampling_ladder as ladder_module
    from camera_platform import lens_sensor_joint_sampling_ladder_job as job_module
    from camera_platform import lens_sensor_registered_roi_disk_raw_job as roi_module
    from camera_platform import lens_sensor_registered_roi_sampling_adapter as adapter_module

    spec = specification(_spec())
    candidate = _candidate(spec)
    baseline = _baseline()
    source_optics = {"raytrace": {"asset_sha256": "source"}}
    candidate_optics = {"raytrace": {"asset_sha256": "candidate"}}
    sensor = Sensor(fields={"stable": True})
    provider = SimpleNamespace(
        optics=source_optics,
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
        "default_worker_timeout_seconds": 1800,
        "submission_preflight_status": "candidate_and_registered_model_worker_preflight_pending",
        "limitation": job_module.LIMITATION,
    }
    registry = SQLiteRegistry(tmp_path / "state")
    service = SimpleNamespace(registry=registry, loaded_environment=baseline["environment"])
    queued = registry.submit(frozen, digest(frozen))
    claimed = registry.claim()
    assert claimed["id"] == queued["id"]
    monkeypatch.setattr(job_module, "_prepare_pending", lambda _service, _spec: frozen)
    monkeypatch.setattr(job_module, "_candidate_binding", lambda _service, _spec: candidate)
    monkeypatch.setattr(roi_module, "_prepare_pending", lambda _service, _roi: baseline)
    monkeypatch.setattr(roi_module, "_model_identities_match_pins", lambda *_args: True)
    monkeypatch.setattr(roi_module, "_recheck_registered_blob", lambda *_args: None)
    monkeypatch.setattr(roi_module, "_execution_inputs", lambda *_args: (
        mode, binding, lens, inspected, provider, sensor, None, None,
    ))
    monkeypatch.setattr(candidate_module, "load_verified_candidate", lambda *_args, **_kwargs: (
        {
            "candidate_psf_array_sha256": candidate["candidate_psf_array_sha256"],
            "source_prescription_sha256": candidate["source_prescription_sha256"],
            "lens_db_sha256": candidate["lens_db_sha256"],
            "lens_revision_content_sha256": candidate["lens_revision_content_sha256"],
        },
        {"psf_function": np.array([1.0])},
    ))
    monkeypatch.setattr(
        sensitivity_module, "optics_with_dense_geometric_psf_candidate",
        lambda *_args: candidate_optics,
    )
    monkeypatch.setattr(
        adapter_module, "frequency_sampling_kwargs_for_registered_roi",
        lambda *_args, **_kwargs: {
            "native_pixel_pitch_um_row_col": (1.22, 1.22),
            "image_distance_m": 0.014,
        },
    )
    calls = []

    def fake_joint(_source, _candidate, _sensor, **kwargs):
        calls.append(kwargs)
        return {
            "status": "inconclusive_joint_scene_grid_refinement",
            "steps": [],
            "independent_pupil_density_convergence_established": False,
            "psf_histogram_pitch_refinement_assessed": False,
            "physical_mtf_comparison_allowed": False,
            "product_ranking_allowed": False,
            "physical_accuracy_qualified": False,
        }

    monkeypatch.setattr(ladder_module, "run_joint_scene_dense_psf_ladder", fake_joint)
    rehashed = []
    original_blob_path = registry.blob_path
    monkeypatch.setattr(
        registry, "blob_path",
        lambda sha: rehashed.append(sha) or tmp_path,
    )
    result = execute(service, claimed)
    assert calls[0]["scene_grid_ladder_row_col"] == spec["joint_goal"][
        "scene_grid_ladder_row_col"
    ]
    assert calls[0]["runner_kwargs"]["coordinate_policy"] == DEFAULT_COORDINATE_POLICY
    assert result["artifacts"][0]["name"] == ARTIFACT_NAME
    assert result["summary"]["physical_accuracy_qualified"] is False
    assert result["summary"]["joint_ladder"]["steps"] == []
    assert rehashed == [candidate["candidate_asset_sha256"]]
    monkeypatch.setattr(registry, "blob_path", original_blob_path)
    registry.finish(claimed["id"], result=result)
    assert get(service, claimed["id"])["result"]["summary"] == result["summary"]
    original_binding = registry.artifact_binding
    monkeypatch.setattr(
        registry, "artifact_binding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            IntegrityError("missing owner binding")
        ),
    )
    with pytest.raises(IntegrityError, match="owner binding"):
        get(service, claimed["id"])
    monkeypatch.setattr(registry, "artifact_binding", original_binding)
