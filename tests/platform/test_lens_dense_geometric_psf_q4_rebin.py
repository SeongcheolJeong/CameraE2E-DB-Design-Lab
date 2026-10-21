"""q4 rebin uses verified q2 rays, fixed support and owner-bound lineage."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from test_lens_dense_geometric_psf_candidate_v2 import _build, _source

from camera_platform import lens_dense_geometric_psf_candidate_v2 as q2
from camera_platform import lens_dense_geometric_psf_candidate_v2_job as q2_job
from camera_platform import lens_dense_geometric_psf_q4_rebin as q4
from camera_platform import lens_dense_geometric_psf_q4_rebin_job as q4_job
from camera_platform import lens_sensor_out_of_core_raw_job as raw_job
from camera_platform.api import create_app
from camera_platform.lens_geometric_psf_rehistogram import _histogram
from camera_platform.lens_geometric_pupil_density_diagnostic import _direct_ray_axis_otf

PARENT_RUN = "run-" + "a" * 32


def _parent(monkeypatch, tmp_path):
    catalog, source, asset, db = _source(monkeypatch, tmp_path)
    report = _build(catalog, tmp_path)
    parent, arrays = q2.load_pinned_dense_geometric_psf_candidate_v2(
        report["report_path"], expected_lens_revision_id="revision-a",
        expected_source_psf_asset_sha256=report["source_psf_asset_sha256"],
    )
    return parent, arrays, (source, asset, db)


def _rebin(parent, arrays, tmp_path, **extra):
    kwargs = {
        "parent_run_id": PARENT_RUN,
        "parent_asset_sha256": parent["candidate_asset_sha256"],
        "output_dir": tmp_path / "q4-candidate",
    }
    kwargs.update(extra)
    return q4.build_q4_geometric_psf_rebin(parent, arrays, **kwargs)


def test_q4_same_rays_exact_support_and_loader(monkeypatch, tmp_path):
    parent, arrays, (source, asset, db) = _parent(monkeypatch, tmp_path)
    original = source["psf_function"].copy()
    report = _rebin(parent, arrays, tmp_path)
    assert report["schema"] == q4.SCHEMA
    assert report["status"] == q4.STATUS
    assert report["same_parent_dense_rays_reused_without_raytrace"] is True
    assert report["source_psf_shape_row_col_field_wave"] == [101, 101, 2, 1]
    assert report["parent_q2_psf_shape_row_col_field_wave"] == [201, 201, 2, 1]
    assert report["candidate_psf_shape_row_col_field_wave"] == [401, 401, 2, 1]
    assert report["candidate_histogram_pitch_mm_row_col"] == [0.00025, 0.00025]
    assert report["physical_sample_center_support_mm_row_col"] == [0.1, 0.1]
    assert report["node_count"] == 2
    assert all(node["q4_fixed_support_retained_ray_fraction"] >= 0.999
               for node in report["node_reports"])
    assert report["pupil_density_convergence_qualified_all_nodes"] is False
    assert report["system_mtf_or_product_ranking_allowed"] is False
    loaded, derived = q4.load_q4_geometric_psf_rebin(
        report["report_path"], expected_parent_run_id=PARENT_RUN,
        expected_parent_asset_sha256=parent["candidate_asset_sha256"],
        expected_lens_revision_id="revision-a",
        expected_source_psf_asset_sha256=parent["source_psf_asset_sha256"],
    )
    assert loaded["candidate_asset_sha256"] == report["candidate_asset_sha256"]
    assert derived["psf_function"].shape == (401, 401, 2, 1)
    np.testing.assert_array_equal(
        derived["dense_admitted_intersections_xy_mm"],
        arrays["dense_admitted_intersections_xy_mm"],
    )
    np.testing.assert_array_equal(source["psf_function"], original)
    assert asset.read_bytes() == b"pinned source asset bytes"
    assert db.read_bytes() == b"pinned source DB bytes"
    assert _rebin(parent, arrays, tmp_path)["candidate_asset_sha256"] == (
        report["candidate_asset_sha256"]
    )


def test_q4_simple_same_ray_gap_is_smaller_than_q2():
    points = np.asarray([[0.0004, 0.0002], [-0.00037, -0.00029]])
    targets = (np.asarray([100., 250., 400.]), np.asarray([100., 250., 400.]))
    direct = tuple(q4._complex_pairs(axis) for axis in _direct_ray_axis_otf(points, targets))
    half = _histogram(points, 201, 0.0005, min_retained=0.999)[0]
    quarter = _histogram(points, 401, 0.00025, min_retained=0.999)[0]
    q2_otf = q4._otf_pairs(half.astype(np.float32), 0.0005, targets)
    q4_otf = q4._otf_pairs(quarter.astype(np.float32), 0.00025, targets)
    assert q4._max_complex_delta(direct, q4_otf) < q4._max_complex_delta(
        direct, q2_otf
    )


def test_q4_budget_refusal_and_tampering(monkeypatch, tmp_path):
    parent, arrays, _source_assets = _parent(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="output-byte policy"):
        _rebin(parent, arrays, tmp_path, max_output_psf_bytes=1)
    assert not (tmp_path / "q4-candidate").exists()
    with pytest.raises(ValueError, match="working-array policy"):
        _rebin(parent, arrays, tmp_path, max_estimated_working_array_bytes=1)
    report = _rebin(parent, arrays, tmp_path)
    with pytest.raises(ValueError, match="identity"):
        q4.load_q4_geometric_psf_rebin(
            report["report_path"], expected_parent_run_id=PARENT_RUN,
            expected_parent_asset_sha256="0" * 64,
            expected_lens_revision_id="revision-a",
            expected_source_psf_asset_sha256=parent["source_psf_asset_sha256"],
        )
    payload = json.loads(Path(report["report_path"]).read_text())
    payload["histogram_target_status"] = "incorrectly_qualified"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="lineage"):
        q4.load_q4_geometric_psf_rebin(
            changed, expected_parent_run_id=PARENT_RUN,
            expected_parent_asset_sha256=parent["candidate_asset_sha256"],
            expected_lens_revision_id="revision-a",
            expected_source_psf_asset_sha256=parent["source_psf_asset_sha256"],
            candidate_asset_path=report["candidate_asset_path"],
        )


def test_q4_job_exact_spec_and_manifest(monkeypatch, tmp_path):
    spec = q4_job.default_spec()
    spec.update({
        "parent_candidate_run_id": PARENT_RUN,
        "parent_candidate_asset_sha256": "a" * 64,
        "lens_revision_id": "4daeca8f-5d81-57b0-8ae3-928f23de2b36",
        "source_psf_asset_sha256": "b" * 64,
    })
    assert q4_job.specification(spec)["schema"] == q4_job.SCHEMA
    with pytest.raises(ValueError, match="exact"):
        q4_job.specification({**spec, "output_dir": "/somewhere"})
    with pytest.raises(ValueError, match="256 MiB"):
        q4_job.specification({**spec, "max_output_psf_bytes": 257 * 1024**2})
    parent, arrays, _source_assets = _parent(monkeypatch, tmp_path)
    report = _rebin(parent, arrays, tmp_path)
    binding = {
        "parent_candidate_run_id": PARENT_RUN,
        "parent_candidate_asset_sha256": parent["candidate_asset_sha256"],
        "parent_candidate_psf_array_sha256": parent["candidate_psf_array_sha256"],
        "parent_dense_intersections_array_sha256": (
            parent["dense_intersections_array_sha256"]
        ),
        "lens_revision_id": parent["lens_revision_id"],
        "lens_revision_content_sha256": parent["lens_revision_content_sha256"],
        "lens_db_sha256": parent["lens_db_sha256"],
        "source_psf_asset_sha256": parent["source_psf_asset_sha256"],
        "source_prescription_sha256": parent["source_prescription_sha256"],
        "simulation_id": parent["simulation_id"],
        "source_psf_shape_row_col_field_wave": parent["source_psf_shape_row_col_field_wave"],
        "target_frequency_cycles_per_mm_row_col": (
            parent["target_frequency_cycles_per_mm_row_col"]
        ),
        "maximum_histogram_complex_otf_abs_delta_allowed": (
            parent["maximum_histogram_complex_otf_abs_delta_allowed"]
        ),
        "parent_source_histogram_pitch_mm_row_col": (
            parent["source_histogram_pitch_mm_row_col"]
        ),
        "parent_q2_histogram_pitch_mm_row_col": (
            parent["candidate_histogram_pitch_mm_row_col"]
        ),
    }
    frozen = {
        **spec,
        "parent_candidate_asset_sha256": parent["candidate_asset_sha256"],
        "lens_revision_id": "revision-a",
        "source_psf_asset_sha256": parent["source_psf_asset_sha256"],
    }
    assert q4_job._manifest_matches(report, frozen, binding)
    bad = dict(report)
    bad["histogram_target_status"] = "incorrectly_qualified"
    assert not q4_job._manifest_matches(bad, frozen, binding)


def test_q4_job_owner_bound_parent_to_published_rebin(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    catalog, _source_data, _asset, _db = _source(monkeypatch, tmp_path)
    revision_id = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"
    original_read = q2._read_source

    def read_source(_revision_id, root):
        identity, arrays, db, asset = original_read("revision-a", root)
        return {**identity, "lens_revision_id": revision_id}, arrays, db, asset

    monkeypatch.setattr(q2, "_read_source", read_source)
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
    monkeypatch.setattr(q2_job, "_pin_lens_source", lambda _service, _spec: pin)
    monkeypatch.setattr(q4_job, "_pin_lens_source", lambda _service, _spec: pin)
    app = create_app(
        tmp_path / "state", Path(__file__).resolve().parents[2],
        start_worker=False, seed_demo=False,
    )
    with TestClient(app):
        service = app.state.service
        parent_spec = q2_job.default_spec()
        parent_spec.update({
            "lens_revision_id": revision_id,
            "pupil_samples": 67,
            "target_histogram_pitch_um": 0.5,
            "target_frequency_cycles_per_mm_row_col": [[5., 10.], [5., 10.]],
        })
        queued_parent = q2_job.submit(service, parent_spec)
        running_parent = service.registry.claim()
        assert running_parent["id"] == queued_parent["id"]
        service.registry.finish(
            queued_parent["id"], result=q2_job.execute(service, running_parent),
        )
        parent_result = q2_job.get(service, queued_parent["id"])
        parent_sha = parent_result["result"]["summary"]["candidate_archive_artifact"]["sha256"]
        spec = q4_job.default_spec()
        spec.update({
            "parent_candidate_run_id": queued_parent["id"],
            "parent_candidate_asset_sha256": parent_sha,
            "lens_revision_id": revision_id,
            "source_psf_asset_sha256": pin["source_psf_asset_sha256"],
        })
        queued = q4_job.submit(service, spec)
        running = service.registry.claim()
        assert running["id"] == queued["id"]
        service.registry.finish(queued["id"], result=q4_job.execute(service, running))
        done = q4_job.get(service, queued["id"])
        assert done["status"] == "succeeded"
        assert done["result"]["summary"]["physical_accuracy_qualified"] is False
        q4_sha = done["result"]["summary"]["candidate_archive_artifact"]["sha256"]
        loaded, arrays = q4_job.load_verified_rebin(
            service, queued["id"], candidate_asset_sha256=q4_sha,
            expected_parent_run_id=queued_parent["id"],
            expected_parent_asset_sha256=parent_sha,
            expected_lens_revision_id=revision_id,
            expected_source_psf_asset_sha256=pin["source_psf_asset_sha256"],
        )
        assert loaded == done["result"]["summary"]["candidate_manifest"]
        assert arrays["psf_function"].shape == (401, 401, 2, 1)
        with pytest.raises(Exception, match="identity"):
            q4_job.load_verified_rebin(
                service, queued["id"], candidate_asset_sha256=q4_sha,
                expected_parent_run_id=queued_parent["id"],
                expected_parent_asset_sha256="0" * 64,
                expected_lens_revision_id=revision_id,
                expected_source_psf_asset_sha256=pin["source_psf_asset_sha256"],
            )
