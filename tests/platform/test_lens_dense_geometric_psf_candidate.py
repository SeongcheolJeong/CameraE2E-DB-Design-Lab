"""The dense candidate is all-node, source-bound and never catalog-promoted."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from camera_platform import lens_dense_geometric_psf_candidate as dense
from camera_platform.lens_geometric_psf_rehistogram import _histogram, _pupil_points


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(monkeypatch, tmp_path):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    source_asset = catalog / "source.npz"
    source_asset.write_bytes(b"pinned source asset bytes")
    source_db = catalog / "lens.db"
    source_db.write_bytes(b"pinned source DB bytes")
    source_pupil = _pupil_points(5)
    fields = [SimpleNamespace(x=0.0, y=0.0), SimpleNamespace(x=1.0, y=0.0)]
    model = {"optical_spec": {"fov": SimpleNamespace(fields=fields)}}

    def trace(_model, location, field, _wave):
        x, y = np.asarray(location, dtype=float)
        return np.asarray([field.x * 0.01 + x * 0.025, y * 0.02]), np.asarray([0., 0., 1.])

    psf = np.zeros((101, 101, 2, 1), dtype=np.float32)
    for field_index, field in enumerate(fields):
        points = np.asarray([trace(model, point, field, 550.)[0] - [field.x * 0.01, 0]
                             for point in source_pupil])
        psf[:, :, field_index, 0] = _histogram(
            points, 101, 0.001, min_retained=0.999
        )[0].astype(np.float32)
    source = {
        "psf_function": psf,
        "field_height_mm": np.asarray([0., 1.]),
        "wavelength_nm": np.asarray([550.]),
        "sample_spacing_mm": np.asarray([0.001, 0.001]),
        "pupil_coordinates": source_pupil,
        "pupil_weights": np.ones((2, 1, len(source_pupil))) / len(source_pupil),
        "success_counts": np.full((2, 1), len(source_pupil)),
        "chief_points_mm": np.asarray([[[0., 0.]], [[0.01, 0.]]]),
        "chief_direction_cosines": np.broadcast_to([0., 0., 1.], (2, 1, 3)).copy(),
        "image_direction_cosines": np.broadcast_to(
            [0., 0., 1.], (2, 1, len(source_pupil), 3)
        ).copy(),
        "quality_json": np.asarray(json.dumps({"psf_kind": "geometric"})),
        "optics_json": np.asarray(json.dumps({"source_pupil_samples": 5})),
        "build_settings_json": np.asarray(json.dumps({
            "pupil_samples": 5,
            "min_retained_energy": 0.999,
            "generator_sha256": "d" * 64,
            "object_distance_m": 1000.0,
        })),
    }
    identity = {
        "lens_revision_id": "revision-a",
        "lens_revision_content_sha256": "a" * 64,
        "simulation_id": "simulation-a",
        "lens_db_sha256": _sha(source_db),
        "source_psf_asset_sha256": _sha(source_asset),
        "source_prescription_sha256": "b" * 64,
        "source_psf_array_sha256": dense._array_sha(psf, "<f4"),
    }
    pinned = SimpleNamespace(
        source_psf_asset_sha256=identity["source_psf_asset_sha256"],
        source_prescription_sha256=identity["source_prescription_sha256"],
        lens_db_sha256=identity["lens_db_sha256"],
        source_pupil=source_pupil,
        model=model,
    )
    monkeypatch.setattr(dense, "_read_source", lambda *_args: (
        identity, source, source_db, source_asset
    ))
    monkeypatch.setattr(dense, "_load_pinned_node", lambda *_args: pinned)
    monkeypatch.setattr(dense, "_trace_point_direction", trace)
    return catalog, source, source_asset, source_db


def _build(catalog, tmp_path, **overrides):
    request = {
        "revision_id": "revision-a",
        "catalog_root": catalog,
        "output_dir": tmp_path / "candidate",
        "pupil_samples": 7,
        "target_frequency_cycles_per_mm_row_col": ((5., 10.), (5., 10.)),
    }
    request.update(overrides)
    return dense.build_pinned_dense_geometric_psf_candidate(**request)


def test_all_nodes_content_addressed_with_fixed_support_and_loader(monkeypatch, tmp_path):
    catalog, source, source_asset, source_db = _fixture(monkeypatch, tmp_path)
    original_psf = source["psf_function"].copy()
    report = _build(catalog, tmp_path)
    assert report["status"] == "all_stored_nodes_candidate_generated_model_only"
    assert report["all_stored_field_wavelength_nodes_completed"] is True
    assert report["source_reproduction_completed_before_any_dense_trace"] is True
    assert report["node_count"] == 2
    assert all(node["source_reproduced_before_dense_trace"] for node in report["node_reports"])
    assert len(report["node_reports"][0]["dense_complex_otf_row_y_up"]) == 2
    assert report["candidate_asset_sha256"] == _sha(Path(report["candidate_asset_path"]))
    assert source_asset.read_bytes() == b"pinned source asset bytes"
    assert source_db.read_bytes() == b"pinned source DB bytes"
    np.testing.assert_array_equal(source["psf_function"], original_psf)
    loaded, arrays = dense.load_pinned_dense_geometric_psf_candidate(
        report["report_path"], expected_lens_revision_id="revision-a",
        expected_source_psf_asset_sha256=report["source_psf_asset_sha256"],
    )
    assert loaded["candidate_asset_sha256"] == report["candidate_asset_sha256"]
    assert arrays["psf_function"].shape == original_psf.shape
    np.testing.assert_array_equal(arrays["sample_spacing_mm"], [0.001, 0.001])
    assert arrays["pupil_coordinates"].shape[0] > source["pupil_coordinates"].shape[0]
    assert arrays["image_direction_cosines"].shape[2] == len(_pupil_points(7))
    assert json.loads(str(arrays["quality_json"]))["candidate_only"] is True
    assert "optics_json" not in arrays
    second = _build(catalog, tmp_path)
    assert second["candidate_asset_sha256"] == report["candidate_asset_sha256"]
    assert second["report_path"] == report["report_path"]


def test_every_source_node_reproduces_before_dense_trace(monkeypatch, tmp_path):
    catalog, source, _source_asset, _source_db = _fixture(monkeypatch, tmp_path)
    source["psf_function"][50, 50, 1, 0] += 0.01
    pupil_sizes = []
    original = dense._trace_pupil

    def record(*args):
        pupil_sizes.append(len(args[3]))
        return original(*args)

    monkeypatch.setattr(dense, "_trace_pupil", record)
    with pytest.raises(ValueError, match="Pinned source PSF did not reproduce at node"):
        _build(catalog, tmp_path)
    assert pupil_sizes == [len(_pupil_points(5)), len(_pupil_points(5))]
    assert not (tmp_path / "candidate").exists()


def test_source_direction_metadata_must_reproduce(monkeypatch, tmp_path):
    catalog, source, _source_asset, _source_db = _fixture(monkeypatch, tmp_path)
    source["image_direction_cosines"][1, 0, 0, 0] += 1e-4
    with pytest.raises(ValueError, match="Pinned source PSF did not reproduce at node"):
        _build(catalog, tmp_path)
    assert not (tmp_path / "candidate").exists()


def test_ray_budget_refuses_before_any_trace(monkeypatch, tmp_path):
    catalog, _source, _source_asset, _source_db = _fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        dense, "_trace_point_direction",
        lambda *_args: pytest.fail("Ray budget must reject before tracing"),
    )
    with pytest.raises(ValueError, match="above declared budget"):
        _build(catalog, tmp_path, max_total_requested_pupil_rays=10)


def test_loader_rejects_identity_tampering_and_supports_relocated_blob(monkeypatch, tmp_path):
    catalog, _source, _source_asset, _source_db = _fixture(monkeypatch, tmp_path)
    report = _build(catalog, tmp_path)
    copied_blob = tmp_path / "other-blob-name.bin"
    copied_blob.write_bytes(Path(report["candidate_asset_path"]).read_bytes())
    sanitized_report = tmp_path / "immutable-manifest.json"
    payload = json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))
    payload.pop("candidate_asset_path")
    payload.pop("report_path", None)
    sanitized_report.write_text(json.dumps(payload), encoding="utf-8")
    loaded, _arrays = dense.load_pinned_dense_geometric_psf_candidate(
        sanitized_report, expected_lens_revision_id="revision-a",
        expected_source_psf_asset_sha256=report["source_psf_asset_sha256"],
        candidate_asset_path=copied_blob,
    )
    assert loaded["candidate_asset_sha256"] == report["candidate_asset_sha256"]
    with pytest.raises(ValueError, match="identity"):
        dense.load_pinned_dense_geometric_psf_candidate(
            sanitized_report, expected_lens_revision_id="wrong-revision",
            expected_source_psf_asset_sha256=report["source_psf_asset_sha256"],
            candidate_asset_path=copied_blob,
        )
    copied_blob.write_bytes(copied_blob.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        dense.load_pinned_dense_geometric_psf_candidate(
            sanitized_report, expected_lens_revision_id="revision-a",
            expected_source_psf_asset_sha256=report["source_psf_asset_sha256"],
            candidate_asset_path=copied_blob,
        )


def test_dense_rays_outside_pinned_support_fail_without_asset(monkeypatch, tmp_path):
    catalog, _source, _source_asset, _source_db = _fixture(monkeypatch, tmp_path)
    normal_trace = dense._trace_point_direction

    def changed_dense_ray(model, location, field, wavelength):
        point, direction = normal_trace(model, location, field, wavelength)
        if np.isclose(float(location[0]), 1 / 3, atol=1e-12, rtol=0):
            point[0] += 0.25
        return point, direction

    monkeypatch.setattr(dense, "_trace_point_direction", changed_dense_ray)
    with pytest.raises(ValueError, match="escaped pinned physical PSF support"):
        _build(catalog, tmp_path)
    assert not (tmp_path / "candidate").exists()


def test_candidate_cannot_be_written_inside_catalog(monkeypatch, tmp_path):
    catalog, _source, _source_asset, _source_db = _fixture(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="outside the production Lens catalog"):
        _build(catalog, tmp_path, output_dir=catalog / "derived")
