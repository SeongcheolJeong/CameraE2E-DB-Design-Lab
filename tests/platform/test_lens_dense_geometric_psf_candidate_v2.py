"""Fine-pitch candidate keeps physical support and exposes, not hides, bin error."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from test_lens_dense_geometric_psf_candidate import _fixture

from camera_platform import lens_dense_geometric_psf_candidate as v1
from camera_platform import lens_dense_geometric_psf_candidate_v2 as v2
from camera_platform import lens_dense_geometric_psf_candidate_v2_job as job_v2
from camera_platform import lens_sensor_out_of_core_raw_job as raw_job
from camera_platform.api import create_app
from camera_platform.lens_geometric_psf_rehistogram import _histogram
from camera_platform.lens_geometric_pupil_density_diagnostic import _direct_ray_axis_otf


def _source(monkeypatch, tmp_path):
    catalog, source, asset, db = _fixture(monkeypatch, tmp_path)
    for name in ("_read_source", "_load_pinned_node", "_trace_point_direction"):
        monkeypatch.setattr(v2, name, getattr(v1, name))
    return catalog, source, asset, db


def _build(catalog, tmp_path, **extra):
    kwargs = {
        "revision_id": "revision-a",
        "catalog_root": catalog,
        "output_dir": tmp_path / "v2-candidate",
        "target_histogram_pitch_um": 0.5,
        "pupil_samples": 7,
        "target_frequency_cycles_per_mm_row_col": ((5., 10.), (5., 10.)),
    }
    kwargs.update(extra)
    return v2.build_pinned_dense_geometric_psf_candidate_v2(**kwargs)


def test_q2_same_physical_support_all_nodes_and_loader(monkeypatch, tmp_path):
    catalog, source, asset, db = _source(monkeypatch, tmp_path)
    original = source["psf_function"].copy()
    report = _build(catalog, tmp_path)
    assert report["schema"] == v2.SCHEMA
    assert report["status"] == v2.STATUS
    assert report["source_reproduction_completed_before_any_dense_trace"] is True
    assert report["all_stored_field_wavelength_nodes_completed"] is True
    assert report["source_psf_shape_row_col_field_wave"] == [101, 101, 2, 1]
    assert report["candidate_psf_shape_row_col_field_wave"] == [201, 201, 2, 1]
    assert report["source_histogram_pitch_mm_row_col"] == [0.001, 0.001]
    assert report["candidate_histogram_pitch_mm_row_col"] == [0.0005, 0.0005]
    assert report["physical_sample_center_support_mm_row_col"] == [0.1, 0.1]
    assert report["node_count"] == 2
    assert all(node["source_reproduced_before_dense_trace"] for node in report["node_reports"])
    assert all(node["dense_source_pitch_retained_ray_fraction"] >= 0.999
               and node["dense_target_pitch_retained_ray_fraction"] >= 0.999
               for node in report["node_reports"])
    assert report["dense_target_pitch_binned_vs_direct_worst_complex_otf_abs_delta"] >= 0
    assert report["pupil_density_convergence_qualified_all_nodes"] is False
    assert report["system_mtf_or_product_ranking_allowed"] is False
    loaded, arrays = v2.load_pinned_dense_geometric_psf_candidate_v2(
        report["report_path"], expected_lens_revision_id="revision-a",
        expected_source_psf_asset_sha256=report["source_psf_asset_sha256"],
    )
    assert loaded["candidate_asset_sha256"] == report["candidate_asset_sha256"]
    assert arrays["psf_function"].shape == (201, 201, 2, 1)
    assert arrays["dense_admitted_intersections_xy_mm"].shape[1] == 2
    assert "optics_json" not in arrays
    np.testing.assert_array_equal(source["psf_function"], original)
    assert asset.read_bytes() == b"pinned source asset bytes"
    assert db.read_bytes() == b"pinned source DB bytes"
    assert _build(catalog, tmp_path)["candidate_asset_sha256"] == report["candidate_asset_sha256"]


def test_same_rays_have_smaller_binning_gap_at_half_pitch():
    points = np.asarray([[0.0004, 0.0002], [-0.00037, -0.00029]])
    targets = (np.asarray([100., 250., 400.]), np.asarray([100., 250., 400.]))
    coarse = _histogram(points, 101, 0.001, min_retained=0.999)[0]
    fine = _histogram(points, 201, 0.0005, min_retained=0.999)[0]
    direct = tuple(v2._complex_pairs(axis) for axis in _direct_ray_axis_otf(points, targets))
    coarse_otf = v2._otf_pairs(coarse.astype(np.float32), 0.001, targets)
    fine_otf = v2._otf_pairs(fine.astype(np.float32), 0.0005, targets)
    assert v2._max_complex_delta(direct, fine_otf) < v2._max_complex_delta(
        direct, coarse_otf
    )


def test_bad_pitch_and_tiny_tolerance_fail_closed_without_qualification(monkeypatch, tmp_path):
    catalog, _source_data, _asset, _db = _source(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="exactly half"):
        _build(catalog, tmp_path, target_histogram_pitch_um=0.4)
    report = _build(catalog, tmp_path, maximum_histogram_complex_otf_abs_delta=1e-12)
    assert report["target_histogram_gap_within_declared_tolerance_model_only"] is False
    assert report["histogram_target_status"] == "inconclusive_target_histogram_gap"


def test_loader_rejects_changed_manifest_pitch_or_archive(monkeypatch, tmp_path):
    catalog, _source_data, _asset, _db = _source(monkeypatch, tmp_path)
    report = _build(catalog, tmp_path)
    payload = json.loads(Path(report["report_path"]).read_text())
    moved = tmp_path / "manifest.json"
    payload["candidate_histogram_pitch_mm_row_col"] = [0.0006, 0.0006]
    moved.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="pitch|lineage"):
        v2.load_pinned_dense_geometric_psf_candidate_v2(
            moved, expected_lens_revision_id="revision-a",
            expected_source_psf_asset_sha256=report["source_psf_asset_sha256"],
            candidate_asset_path=report["candidate_asset_path"],
        )
    original_payload = json.loads(Path(report["report_path"]).read_text())
    moved.write_text(json.dumps(original_payload))
    bad_blob = tmp_path / "bad.npz"
    bad_blob.write_bytes(Path(report["candidate_asset_path"]).read_bytes() + b"changed")
    with pytest.raises(ValueError, match="integrity"):
        v2.load_pinned_dense_geometric_psf_candidate_v2(
            moved, expected_lens_revision_id="revision-a",
            expected_source_psf_asset_sha256=report["source_psf_asset_sha256"],
            candidate_asset_path=bad_blob,
        )

    # Forge a self-consistent archive/manifest pair with only the status text
    # changed. The numeric decision must still be recomputed on load.
    forged = dict(original_payload)
    forged["histogram_target_status"] = (
        "inconclusive_target_histogram_gap"
        if original_payload["target_histogram_gap_within_declared_tolerance_model_only"]
        else "numeric_gap_within_declared_tolerance_model_only"
    )
    with np.load(report["candidate_asset_path"], allow_pickle=False) as archive:
        contents = {name: np.asarray(archive[name]).copy() for name in archive.files}
    embedded = json.loads(str(contents["candidate_metadata_json"]))
    embedded["histogram_target_status"] = forged["histogram_target_status"]
    contents["candidate_metadata_json"] = np.asarray(json.dumps(embedded))
    rewritten = tmp_path / "forged.npz"
    np.savez_compressed(rewritten, **contents)
    forged_sha = v2._file_sha(rewritten)
    forged["candidate_asset_sha256"] = forged_sha
    forged["candidate_asset_file"] = forged_sha + ".npz"
    moved.write_text(json.dumps(forged))
    with pytest.raises(ValueError, match="decision"):
        v2.load_pinned_dense_geometric_psf_candidate_v2(
            moved, expected_lens_revision_id="revision-a",
            expected_source_psf_asset_sha256=report["source_psf_asset_sha256"],
            candidate_asset_path=rewritten,
        )


def test_v2_job_spec_is_exact_and_manifest_preserves_failed_gap(monkeypatch, tmp_path):
    spec = job_v2.default_spec()
    spec["lens_revision_id"] = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"
    spec["target_histogram_pitch_um"] = 0.5
    assert job_v2.specification(spec)["target_histogram_pitch_um"] == 0.5
    with pytest.raises(ValueError, match="exact"):
        job_v2.specification({**spec, "output_dir": "/somewhere"})
    with pytest.raises(ValueError, match="pitch"):
        job_v2.specification({**spec, "target_histogram_pitch_um": True})

    catalog, _source_data, _asset, _db = _source(monkeypatch, tmp_path)
    report = _build(catalog, tmp_path, maximum_histogram_complex_otf_abs_delta=1e-12)
    frozen = {
        **spec,
        "lens_revision_id": "revision-a",
        "pupil_samples": 7,
        "target_frequency_cycles_per_mm_row_col": [[5., 10.], [5., 10.]],
        "maximum_histogram_complex_otf_abs_delta": 1e-12,
    }
    pin = {
        "lens_revision_id": "revision-a",
        "lens_revision_sha256": report["lens_revision_content_sha256"],
        "simulation_id": report["simulation_id"],
        "lens_db_sha256": report["lens_db_sha256"],
        "source_psf_asset_sha256": report["source_psf_asset_sha256"],
        "source_prescription_sha256": report["source_prescription_sha256"],
    }
    assert job_v2._manifest_matches(report, frozen, pin)
    bad = dict(report)
    bad["histogram_refinement_factor"] = 1
    assert not job_v2._manifest_matches(bad, frozen, pin)
    bad = dict(report)
    bad["histogram_target_status"] = "numeric_gap_within_declared_tolerance_model_only"
    assert not job_v2._manifest_matches(bad, frozen, pin)


def test_v2_job_publishes_and_reloads_owner_bound_artifacts(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    catalog, _source_data, _asset, _db = _source(monkeypatch, tmp_path)
    revision_id = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"
    original_read = v2._read_source

    def read_source(_revision_id, root):
        identity, arrays, db, asset = original_read("revision-a", root)
        return {**identity, "lens_revision_id": revision_id}, arrays, db, asset

    monkeypatch.setattr(v2, "_read_source", read_source)
    monkeypatch.setattr(raw_job, "_db_root", lambda _service: catalog)
    identity, _arrays, _db_path, _asset_path = read_source(revision_id, catalog)
    pin = {
        "lens_revision_id": revision_id,
        "lens_revision_sha256": identity["lens_revision_content_sha256"],
        "simulation_id": identity["simulation_id"],
        "lens_db_sha256": identity["lens_db_sha256"],
        "source_psf_asset_sha256": identity["source_psf_asset_sha256"],
        "source_prescription_sha256": identity["source_prescription_sha256"],
    }
    monkeypatch.setattr(job_v2, "_pin_lens_source", lambda _service, _spec: pin)
    app = create_app(
        tmp_path / "state", Path(__file__).resolve().parents[2],
        start_worker=False, seed_demo=False,
    )
    with TestClient(app):
        service = app.state.service
        spec = job_v2.default_spec()
        spec.update({
            "lens_revision_id": revision_id,
            "pupil_samples": 67,
            "target_histogram_pitch_um": 0.5,
            "target_frequency_cycles_per_mm_row_col": [[5., 10.], [5., 10.]],
        })
        queued = job_v2.submit(service, spec)
        assert queued["status"] == "queued"
        running = service.registry.claim()
        assert running["id"] == queued["id"]
        result = job_v2.execute(service, running)
        service.registry.finish(queued["id"], result=result)
        completed = job_v2.get(service, queued["id"])
        assert completed["status"] == "succeeded"
        summary = completed["result"]["summary"]
        assert summary["physical_accuracy_qualified"] is False
        assert "candidate_asset_path" not in summary["candidate_manifest"]
        manifest, arrays = job_v2.load_verified_candidate(
            service, queued["id"],
            candidate_asset_sha256=summary["candidate_archive_artifact"]["sha256"],
            expected_lens_revision_id=revision_id,
            expected_source_psf_asset_sha256=pin["source_psf_asset_sha256"],
        )
        assert manifest == summary["candidate_manifest"]
        assert arrays["psf_function"].shape == (201, 201, 2, 1)
