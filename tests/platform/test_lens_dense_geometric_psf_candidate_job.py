"""Pinned all-node candidate lifecycle; producer physics has separate tests."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform import lens_dense_geometric_psf_candidate as producer
from camera_platform import lens_dense_geometric_psf_candidate_job as job_module
from camera_platform.api import create_app
from camera_platform.registry import IntegrityError
from camera_platform.sdk import PlatformClient
from camera_platform.worker import execute as worker_execute

WORKSPACE = Path(__file__).resolve().parents[2]
REVISION_ID = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"


def _spec():
    spec = job_module.default_spec()
    spec["lens_revision_id"] = REVISION_ID
    spec["target_frequency_cycles_per_mm_row_col"] = [[100.0, 250.0], [100.0, 250.0]]
    return spec


def _pin():
    return {
        "lens_revision_id": REVISION_ID,
        "lens_revision_sha256": "a" * 64,
        "lens_db_sha256": "b" * 64,
        "simulation_id": "sim-pinned",
        "source_psf_asset_sha256": "c" * 64,
        "source_prescription_sha256": "d" * 64,
        "reviewed_prescription_file_sha256": "e" * 64,
        "catalog_artifact_id": "artifact-pinned",
    }


def _fake_build(revision_id, *, output_dir, **kwargs):
    assert revision_id == REVISION_ID
    assert kwargs["pupil_samples"] == 145
    destination = Path(output_dir)
    temporary = destination / "candidate.npz"
    np.savez_compressed(temporary, psf_function=np.ones((3, 3, 2, 1), dtype=np.float32))
    archive_sha = hashlib.sha256(temporary.read_bytes()).hexdigest()
    archive = destination / f"{archive_sha}.npz"
    temporary.rename(archive)
    nodes = [
        {
            "field_index": field_index,
            "field_height_mm": float(field_index),
            "wavelength_index": 0,
            "wavelength_nm": 550.0,
            "source_reproduced_before_dense_trace": True,
        }
        for field_index in range(2)
    ]
    report = {
        "schema": producer.SCHEMA,
        "status": "all_stored_nodes_candidate_generated_model_only",
        "lens_revision_id": REVISION_ID,
        "lens_revision_content_sha256": "a" * 64,
        "simulation_id": "sim-pinned",
        "lens_db_sha256": "b" * 64,
        "source_psf_asset_sha256": "c" * 64,
        "source_prescription_sha256": "d" * 64,
        "candidate_asset_sha256": archive_sha,
        "candidate_asset_file": archive.name,
        "candidate_psf_array_sha256": "f" * 64,
        "source_psf_shape_row_col_field_wave": [3, 3, 2, 1],
        "candidate_psf_shape_row_col_field_wave": [3, 3, 2, 1],
        "fixed_physical_histogram_pitch_mm_row_col": [0.0006, 0.0006],
        "field_height_mm": [0.0, 1.0],
        "wavelength_nm": [550.0],
        "dense_pupil_samples": kwargs["pupil_samples"],
        "target_frequency_cycles_per_mm_row_col": [
            list(axis) for axis in kwargs["target_frequency_cycles_per_mm_row_col"]
        ],
        "node_count": 2,
        "node_reports": nodes,
        "all_stored_field_wavelength_nodes_completed": True,
        "source_reproduction_completed_before_any_dense_trace": True,
        "output_psf_float32_bytes": 3 * 3 * 2 * 1 * 4,
        "requested_pupil_rays": 200,
        "limits": {
            "max_total_requested_pupil_rays": kwargs["max_total_requested_pupil_rays"],
            "max_output_psf_bytes": kwargs["max_output_psf_bytes"],
            "soft_timeout_s": kwargs["soft_timeout_s"],
        },
        "optical_psf_semantics": "geometric_pre_pixel_ray_histogram",
        "diffraction_included": False,
        "pixel_aperture_included": False,
        "pupil_density_convergence_qualified_all_nodes": False,
        "system_mtf_or_product_ranking_allowed": False,
        "candidate_asset_path": str(archive),
    }
    report_path = destination / f"{archive_sha}.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return {**report, "report_path": str(report_path)}


def _sdk(transport):
    sdk = object.__new__(PlatformClient)
    sdk.http = transport
    return sdk


def test_specification_is_explicit_and_rejects_paths():
    spec = job_module.specification(_spec())
    assert spec["schema"] == job_module.SCHEMA
    assert job_module.WORKER_TIMEOUT_S > spec["soft_timeout_s"]
    altered = deepcopy(spec)
    altered["output_dir"] = "/private/tmp/out"
    with pytest.raises(ValueError, match="exact"):
        job_module.specification(altered)
    altered = deepcopy(spec)
    altered["pupil_samples"] = True
    with pytest.raises(ValueError, match="pupil samples"):
        job_module.specification(altered)
    altered = deepcopy(spec)
    altered["target_frequency_cycles_per_mm_row_col"][0] = [250, 100]
    with pytest.raises(ValueError, match="increase"):
        job_module.specification(altered)


def test_api_sdk_worker_publish_two_typed_path_free_artifacts(tmp_path, monkeypatch):
    pin = _pin()
    monkeypatch.setattr(job_module, "_pin_lens_source", lambda _service, _spec: pin)
    monkeypatch.setattr(producer, "build_pinned_dense_geometric_psf_candidate", _fake_build)
    monkeypatch.setattr(
        producer, "load_pinned_dense_geometric_psf_candidate",
        lambda report_path, **_kwargs: (json.loads(Path(report_path).read_text()), {}),
    )
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
        sdk = _sdk(transport)
        assert sdk.lens_dense_geometric_psf_candidate_template()["specification"]["schema"] == (
            job_module.SCHEMA
        )
        submitted = sdk.submit_lens_dense_geometric_psf_candidate(_spec())
        assert submitted["status"] == "queued"
        assert submitted["pinned_source_binding"] == pin
        assert sdk.submit_lens_dense_geometric_psf_candidate(_spec())["id"] == submitted["id"]
        assert app.state.service.registry.claim()["id"] == submitted["id"]
        worker_execute(app.state.service, submitted["id"])
        completed = sdk.wait_lens_dense_geometric_psf_candidate(
            submitted["id"], timeout=5, poll_interval=0.01
        )
        assert completed["status"] == "succeeded", completed["error"]
        summary = completed["result"]["summary"]
        assert summary["candidate_manifest"]["node_count"] == 2
        assert "candidate_asset_path" not in summary["candidate_manifest"]
        assert "report_path" not in summary["candidate_manifest"]
        assert str(tmp_path) not in json.dumps(summary)
        assert summary["physical_accuracy_qualified"] is False
        assert [entry["name"] for entry in completed["artifacts"]] == [
            job_module.NPZ_NAME, job_module.MANIFEST_NAME,
        ]
        assert transport.get(f"/api/v1/runs/{submitted['id']}").status_code == 404
        assert any(
            item["id"] == submitted["id"]
            for item in sdk.list_lens_dense_geometric_psf_candidates()
        )
        assert transport.get(
            f"/api/v1/lens-dense-geometric-psf-candidates/{submitted['id']}/export"
        ).status_code == 200
        artifact = summary["candidate_archive_artifact"]
        binding = app.state.service.registry.artifact_binding(
            artifact["sha256"], owner_kind="runs", owner_id=submitted["id"]
        )
        assert binding["descriptor"] == artifact
        manifest, arrays = job_module.load_verified_candidate(
            app.state.service, submitted["id"],
            candidate_asset_sha256=artifact["sha256"],
            expected_lens_revision_id=REVISION_ID,
            expected_source_psf_asset_sha256=pin["source_psf_asset_sha256"],
        )
        assert manifest == summary["candidate_manifest"]
        assert arrays == {}
        with pytest.raises(IntegrityError, match="Candidate SHA"):
            job_module.load_verified_candidate(
                app.state.service, submitted["id"],
                candidate_asset_sha256="0" * 64,
                expected_lens_revision_id=REVISION_ID,
                expected_source_psf_asset_sha256=pin["source_psf_asset_sha256"],
            )
        with pytest.raises(IntegrityError, match="catalog Lens"):
            job_module.load_verified_candidate(
                app.state.service, submitted["id"],
                candidate_asset_sha256=artifact["sha256"],
                expected_lens_revision_id=REVISION_ID,
                expected_source_psf_asset_sha256="0" * 64,
            )
        altered = deepcopy(completed)
        altered["result"]["summary"]["candidate_manifest"]["source_psf_asset_sha256"] = (
            "0" * 64
        )
        with monkeypatch.context() as guard:
            guard.setattr(app.state.service.registry, "get_run", lambda _identity: altered)
            with pytest.raises(IntegrityError, match="frozen request"):
                job_module.get(app.state.service, submitted["id"])


def test_worker_rejects_changed_source_before_ray_trace(tmp_path, monkeypatch):
    pin = _pin()
    monkeypatch.setattr(job_module, "_pin_lens_source", lambda _service, _spec: pin)
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
        sdk = _sdk(transport)
        submitted = sdk.submit_lens_dense_geometric_psf_candidate(_spec())
        app.state.service.registry.claim()
        monkeypatch.setattr(
            job_module, "_pin_lens_source",
            lambda _service, _spec: {**pin, "source_psf_asset_sha256": "0" * 64},
        )
        monkeypatch.setattr(
            producer, "build_pinned_dense_geometric_psf_candidate",
            lambda *_args, **_kwargs: pytest.fail("Ray tracing must not begin after source drift"),
        )
        worker_execute(app.state.service, submitted["id"])
        failed = sdk.lens_dense_geometric_psf_candidate(submitted["id"])
        assert failed["status"] == "failed"
        assert "Pinned dense geometric PSF request changed" in failed["error"]
