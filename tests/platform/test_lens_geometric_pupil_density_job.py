"""Public pinned geometric pupil-density Job contract and bounded lifecycle."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from camera_platform import lens_geometric_pupil_density_diagnostic as diagnostic_module
from camera_platform import lens_geometric_pupil_density_job as job_module
from camera_platform.api import create_app
from camera_platform.registry import IntegrityError
from camera_platform.sdk import PlatformClient
from camera_platform.worker import execute as worker_execute

WORKSPACE = Path(__file__).resolve().parents[2]
REVISION_ID = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"


def _spec():
    spec = job_module.default_spec()
    spec["lens_revision_id"] = REVISION_ID
    spec["higher_pupil_samples"] = [81, 97, 113]
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


def _diagnostic(spec, pin):
    def otf(value):
        return [[value, 0.0], [value, 0.0]]

    return {
        "schema": diagnostic_module.SCHEMA,
        "status": "dense_geometric_pupil_reference_stable_at_declared_targets_model_only",
        "lens_revision_id": spec["lens_revision_id"],
        "simulation_id": pin["simulation_id"],
        "lens_db_sha256": pin["lens_db_sha256"],
        "source_psf_asset_sha256": pin["source_psf_asset_sha256"],
        "source_prescription_sha256": pin["source_prescription_sha256"],
        "field_index": spec["field_index"],
        "wavelength_nm": spec["wavelength_nm"],
        "higher_pupil_samples": spec["higher_pupil_samples"],
        "target_frequency_cycles_per_mm_row_col": (
            spec["target_frequency_cycles_per_mm_row_col"]
        ),
        "max_total_requested_pupil_rays": spec["max_total_requested_pupil_rays"],
        "maximum_complex_otf_delta_allowed": spec["maximum_complex_otf_delta"],
        "maximum_admitted_fraction_delta_allowed": spec[
            "maximum_admitted_fraction_delta"
        ],
        "soft_timeout_s": spec["soft_timeout_s"],
        "required_consecutive_refinement_passes": 2,
        "dense_refinement_baseline": "first_higher_pupil_grid",
        "pinned_source_pupil_density_adequacy_status": (
            "adequate_at_declared_targets_geometric_model_only"
        ),
        "pinned_source_pupil_density_adequate_at_declared_targets": True,
        "pinned_source_pupil_vs_stable_dense_ray_reference": {
            "source_pupil_samples": 65,
            "dense_reference_pupil_samples": 113,
            "maximum_complex_otf_delta_row_y_up": 0.004,
            "maximum_complex_otf_delta_col_x_right": 0.004,
            "maximum_complex_otf_delta": 0.004,
            "admitted_pupil_fraction_delta": 0.0,
            "declared_numeric_tolerances_met": True,
        },
        "saved_psf_histogram_sampling_qualified": False,
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
        "diffraction_included": False,
        "pixel_aperture_included": False,
        "psf_histogram_interpolation_used_for_otf": False,
        "steps": [
            {
                "pupil_samples": 65, "source_histogram_reproduced": True,
                "consecutive_passes": 0, "admitted_pupil_fraction": 1.0,
                "complex_otf_row_y_up": otf(0.505),
                "complex_otf_col_x_right": otf(0.505),
            },
            {
                "pupil_samples": 81, "consecutive_passes": 0,
                "comparison_role": "source_to_first_higher_grid_baseline",
                "admitted_pupil_fraction": 1.0,
                "complex_otf_row_y_up": otf(0.507),
                "complex_otf_col_x_right": otf(0.507),
            },
            {
                "pupil_samples": 97, "consecutive_passes": 1,
                "comparison_role": "dense_refinement",
                "declared_numeric_tolerances_met": True,
                "maximum_complex_otf_delta_from_previous": 0.001,
                "admitted_pupil_fraction_delta_from_previous": 0.0,
                "admitted_pupil_fraction": 1.0,
                "complex_otf_row_y_up": otf(0.508),
                "complex_otf_col_x_right": otf(0.508),
            },
            {
                "pupil_samples": 113, "consecutive_passes": 2,
                "comparison_role": "dense_refinement",
                "declared_numeric_tolerances_met": True,
                "maximum_complex_otf_delta_from_previous": 0.001,
                "admitted_pupil_fraction_delta_from_previous": 0.0,
                "admitted_pupil_fraction": 1.0,
                "complex_otf_row_y_up": otf(0.509),
                "complex_otf_col_x_right": otf(0.509),
            },
        ],
    }


def test_exact_versioned_request_bounds_and_model_only_contract():
    accepted = job_module.specification(_spec())
    assert accepted["schema"] == job_module.SCHEMA
    assert accepted["soft_timeout_s"] <= 300
    assert job_module.WORKER_TIMEOUT_S > accepted["soft_timeout_s"]
    invalid = deepcopy(accepted)
    invalid["field_index"] = True
    with pytest.raises(ValueError, match="field index"):
        job_module.specification(invalid)
    invalid = deepcopy(accepted)
    invalid["target_frequency_cycles_per_mm_row_col"][0] = [250.0, 100.0]
    with pytest.raises(ValueError, match="increase"):
        job_module.specification(invalid)
    invalid = deepcopy(accepted)
    invalid["max_total_requested_pupil_rays"] = 50_001
    with pytest.raises(ValueError, match="Ray budget"):
        job_module.specification(invalid)
    invalid = deepcopy(accepted)
    invalid["local_path"] = "/private/hidden"
    with pytest.raises(ValueError, match="exact"):
        job_module.specification(invalid)


def test_saved_lens_source_pin_is_lightweight_and_bounded():
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned local Lens DB unavailable")
    pin = job_module._pin_lens_source(
        SimpleNamespace(workspace=WORKSPACE), job_module.specification(_spec())
    )
    assert pin["lens_revision_id"] == REVISION_ID
    assert pin["simulation_id"]
    assert all(
        len(pin[field]) == 64
        for field in (
            "lens_db_sha256", "source_psf_asset_sha256",
            "reviewed_prescription_file_sha256",
        )
    )


def test_api_sdk_worker_dispatch_and_typed_artifact_validation(tmp_path, monkeypatch):
    pin = _pin()
    monkeypatch.setattr(job_module, "_pin_lens_source", lambda _service, _spec: pin)

    def fake_diagnostic(revision_id, **kwargs):
        assert revision_id == REVISION_ID
        assert kwargs["catalog_root"] == WORKSPACE / "camerae2e_db"
        assert kwargs["field_index"] == 0
        return _diagnostic(job_module.specification(_spec()), pin)

    monkeypatch.setattr(
        diagnostic_module, "diagnose_pinned_geometric_pupil_density", fake_diagnostic
    )
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = transport
        assert sdk.lens_geometric_pupil_density_template()["specification"]["schema"] == (
            job_module.SCHEMA
        )
        submitted = sdk.submit_lens_geometric_pupil_density(_spec())
        assert submitted["status"] == "queued"
        assert submitted["pinned_source_binding"] == pin
        assert submitted["submission_preflight_status"].endswith("worker_preflight_pending")
        assert sdk.submit_lens_geometric_pupil_density(_spec())["id"] == submitted["id"]
        claimed = app.state.service.registry.claim()
        assert claimed["id"] == submitted["id"]
        worker_execute(app.state.service, submitted["id"])
        completed = sdk.wait_lens_geometric_pupil_density(
            submitted["id"], timeout=5, poll_interval=0.01
        )
        assert completed["status"] == "succeeded", completed["error"]
        assert completed["result"]["summary"]["status"].endswith("model_only")
        assert completed["result"]["summary"][
            "pinned_source_pupil_density_adequate_at_declared_targets"
        ] is True
        assert completed["result"]["summary"]["physical_accuracy_qualified"] is False
        assert len(completed["result"]["artifacts"]) == 1
        assert transport.get(f"/api/v1/runs/{submitted['id']}").status_code == 404
        assert any(
            item["id"] == submitted["id"]
            for item in sdk.list_lens_geometric_pupil_density()
        )
        assert transport.get(
            f"/api/v1/lens-geometric-pupil-density/{submitted['id']}/export"
        ).status_code == 200
        altered = deepcopy(completed)
        altered["result"]["summary"]["diagnostic"]["source_psf_asset_sha256"] = "0" * 64
        with monkeypatch.context() as guard:
            guard.setattr(app.state.service.registry, "get_run", lambda _identity: altered)
            with pytest.raises(IntegrityError, match="frozen request"):
                job_module.get(app.state.service, submitted["id"])


def test_contract_rejects_conflating_dense_stability_with_source_adequacy():
    spec = job_module.specification(_spec())
    pin = _pin()
    accepted = _diagnostic(spec, pin)
    assert job_module._diagnostic_matches(accepted, spec, pin)
    false_source_claim = deepcopy(accepted)
    false_source_claim["pinned_source_pupil_density_adequate_at_declared_targets"] = False
    assert not job_module._diagnostic_matches(false_source_claim, spec, pin)
    false_delta = deepcopy(accepted)
    false_delta["pinned_source_pupil_vs_stable_dense_ray_reference"][
        "maximum_complex_otf_delta"
    ] = 0.09
    assert not job_module._diagnostic_matches(false_delta, spec, pin)
    false_dense_refinement = deepcopy(accepted)
    false_dense_refinement["steps"][-2]["comparison_role"] = (
        "source_to_first_higher_grid_baseline"
    )
    assert not job_module._diagnostic_matches(false_dense_refinement, spec, pin)


def test_completed_v1_artifact_remains_readable_without_new_adequacy_claim(tmp_path):
    spec = job_module.specification(_spec())
    legacy_spec = {**spec, "schema": job_module.LEGACY_SCHEMA}
    with pytest.raises(ValueError, match="Unsupported"):
        job_module.specification(legacy_spec)
    pin = _pin()
    diagnostic = _diagnostic(spec, pin)
    diagnostic.update(
        schema="camera_pinned_geometric_pupil_density_diagnostic_v1",
        status="geometric_pupil_density_stable_at_declared_targets_model_only",
    )
    for key in (
        "dense_refinement_baseline", "pinned_source_pupil_density_adequacy_status",
        "pinned_source_pupil_density_adequate_at_declared_targets",
        "pinned_source_pupil_vs_stable_dense_ray_reference",
        "saved_psf_histogram_sampling_qualified",
    ):
        diagnostic.pop(key)
    summary = {
        "schema": job_module.LEGACY_SCHEMA,
        "status": diagnostic["status"],
        "specification": legacy_spec,
        "frozen_submission_sha256": "f" * 64,
        "pinned_source_binding": pin,
        "diagnostic": diagnostic,
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
        "physical_accuracy_qualified": False,
    }
    blob = tmp_path / "legacy-result.json"
    blob.write_text(json.dumps(summary))
    job = {
        "id": "legacy-job", "job_kind": job_module.JOB_KIND, "status": "succeeded",
        "specification": legacy_spec, "pinned_source_binding": pin,
        "content_hash": "f" * 64,
        "result": {
            "summary": summary,
            "artifacts": [{"name": job_module.ARTIFACT_NAME, "sha256": "a" * 64}],
        },
    }
    registry = SimpleNamespace(
        get_run=lambda _identity: job,
        blob_path=lambda _sha256: blob,
    )
    assert job_module.get(SimpleNamespace(registry=registry), "legacy-job") == job
    assert "pinned_source_pupil_density_adequacy_status" not in summary


def test_changed_pin_blocks_worker_before_diagnostic(tmp_path, monkeypatch):
    pin = _pin()
    monkeypatch.setattr(job_module, "_pin_lens_source", lambda _service, _spec: pin)
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = transport
        submitted = sdk.submit_lens_geometric_pupil_density(_spec())
        app.state.service.registry.claim()

        def changed_pin(_service, _spec):
            return {**pin, "source_psf_asset_sha256": "0" * 64}

        monkeypatch.setattr(job_module, "_pin_lens_source", changed_pin)
        monkeypatch.setattr(
            diagnostic_module, "diagnose_pinned_geometric_pupil_density",
            lambda *_args, **_kwargs: pytest.fail("Ray tracing must not begin after pin drift"),
        )
        worker_execute(app.state.service, submitted["id"])
        failed = sdk.lens_geometric_pupil_density(submitted["id"])
        assert failed["status"] == "failed"
        assert "Pinned geometric pupil-density request changed" in failed["error"]
