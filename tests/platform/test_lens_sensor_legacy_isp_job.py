"""CameraE2E spectral ISP reads exactly the retained Lens x Sensor RAW."""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from camera_platform import lens_sensor_legacy_isp_job as job
from camera_platform import lens_sensor_raw_job as raw_job
from camera_platform.sensor_db_bridge import sensor_revision_catalog
from camera_platform.service import PlatformService

WORKSPACE = Path(__file__).resolve().parents[2]
ROOT = WORKSPACE / "camerae2e_db"
P0248_REVISION = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"


def test_spec_requires_a_bounded_method_and_explicit_color_limit_acknowledgement():
    spec = job.default_spec()
    spec["source_lens_sensor_job_id"] = "saved-run"
    with pytest.raises(ValueError, match="acknowledgement"):
        job.specification(spec)
    spec["acknowledge_model_derived_color_limits"] = True
    assert job.specification(spec)["demosaic_method"] == "bilinear"
    with pytest.raises(ValueError, match="demosaic method"):
        job.specification(dict(spec, demosaic_method="pocs"))


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    if not (ROOT / "catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    service = PlatformService(tmp_path_factory.mktemp("legacy-isp-job-state"), WORKSPACE)
    sensor = next(
        row for row in sensor_revision_catalog(root=ROOT)
        if row["report_code"] == "DEF-2512-801" and row["model"] == "db_estimate_v1"
    )
    spec = raw_job.default_bounded_spec()
    spec.update(
        lens_revision_id=P0248_REVISION,
        sensor_revision={"identifier": "DEF-2512-801", "run_id": sensor["run_id"]},
        scene_kind="slanted_edge", sensor_size=[16, 16], noise_flag=2,
        accept_approximate_lens_radiometry=True,
    )
    queued = raw_job.submit(service, spec)
    assert service.registry.claim()["id"] == queued["id"]
    service.registry.finish(queued["id"], result=raw_job.execute(service, queued), error=None)
    saved = raw_job.get(service, queued["id"])
    assert saved["status"] == "succeeded"
    return service, saved


def test_saved_d65_source_uses_camerae2e_spectral_isp_without_changing_raw(source):
    service, saved = source
    app = FastAPI()
    job.install_routes(app, service)
    with TestClient(app) as transport:
        template = transport.get(
            "/api/v1/lens-sensor-legacy-isp/template",
            params={"source_lens_sensor_job_id": saved["id"]},
        )
        assert template.status_code == 200, template.text
        body = template.json()
        assert body["selected_source_contract"]["source_scene"]["kind"] == (
            "slanted_edge_d65"
        )
        assert body["demosaic_methods"] == [
            "bilinear", "laplacian", "adaptive_laplacian"
        ]
        spec = body["specification"]
        spec.update(
            demosaic_method="adaptive_laplacian",
            acknowledge_model_derived_color_limits=True,
        )
        response = transport.post("/api/v1/lens-sensor-legacy-isp", json=spec)
        assert response.status_code == 202, response.text
        queued = response.json()
        assert queued["sensor_model_snapshot_sha256"]
        assert queued["method_binding"]["demosaic_method"] == "adaptive_laplacian"
        assert service.registry.claim()["id"] == queued["id"]
        service.registry.finish(queued["id"], result=job.execute(service, queued), error=None)
        detail = transport.get(f"/api/v1/lens-sensor-legacy-isp/{queued['id']}")
        assert detail.status_code == 200, detail.text
        finished = detail.json()
        summary = finished["result"]["summary"]
        report = summary["report"]
        assert report["color_contract"]["measured_color_accuracy"] is False
        assert report["color_contract"]["sensor_spectral_response"] == (
            "saved_db_estimate_v1_aggregate_eqe"
        )
        assert report["raw_values_byte_equal"] is True
        assert report["source_binding"] == queued["source_binding"]
        original = np.load(
            service.registry.blob_path(
                saved["result"]["summary"]["preview"]["raw_artifact"]["sha256"]
            ), allow_pickle=False,
        )
        npz = service.registry.blob_path(summary["evidence"]["npz"]["sha256"]).read_bytes()
        with np.load(io.BytesIO(npz), allow_pickle=False) as arrays:
            assert arrays["source_raw_dn_frame"].dtype == np.dtype("int32")
            assert arrays["source_raw_dn_frame"].tobytes(order="C") == original.tobytes(
                order="C"
            )
            assert arrays["model_derived_xyz"].shape == (16, 16, 3)
            assert arrays["srgb"].shape == (16, 16, 3)
            assert not np.array_equal(arrays["model_derived_xyz"], arrays["sensor_rgb"])
        exported = transport.get(f"/api/v1/lens-sensor-legacy-isp/{queued['id']}/export")
        assert exported.status_code == 200
        assert json.loads(exported.content)["result"]["summary"] == summary


def test_demosaic_methods_keep_one_model_derived_color_transform(source):
    from camera_platform.lens_sensor_legacy_isp import evaluate_lens_sensor_legacy_isp
    from camera_platform.retained_raw_dataset_job import _lens_source

    service, saved = source
    record = _lens_source(service, {"source_job_id": saved["id"]})
    report_path = service.registry.blob_path(record["source_artifacts"]["json"]["sha256"])
    raw_path = service.registry.blob_path(record["source_artifacts"]["raw"]["sha256"])
    results = {}
    for method in ("bilinear", "laplacian", "adaptive_laplacian"):
        results[method] = evaluate_lens_sensor_legacy_isp(
            report_path, raw_path,
            source_lens_sensor_job_id=saved["id"],
            sensor_db_root=ROOT, workspace=WORKSPACE,
            demosaic_method=method,
        )
    raw_hashes = {result["summary"]["raw_content_sha256"] for result in results.values()}
    assert len(raw_hashes) == 1
    matrices = {
        tuple(map(tuple, result["summary"]["color_contract"]["sensor_to_xyz_matrix"]))
        for result in results.values()
    }
    assert len(matrices) == 1
    assert not np.array_equal(
        results["bilinear"]["arrays"]["sensor_rgb"],
        results["adaptive_laplacian"]["arrays"]["sensor_rgb"],
    )
    assert all(
        result["summary"]["color_contract"]["measured_color_accuracy"] is False
        for result in results.values()
    )


def test_nested_camerae2e_display_uses_pinned_offline_assets(source, monkeypatch):
    from camera_platform.lens_sensor_legacy_isp import evaluate_lens_sensor_legacy_isp
    from camera_platform.retained_raw_dataset_job import _lens_source
    from pyisetcam import assets as legacy_assets

    service, saved = source
    record = _lens_source(service, {"source_job_id": saved["id"]})

    def forbid_download():
        raise AssertionError("Nested CameraE2E display attempted an upstream download")

    monkeypatch.setattr(legacy_assets, "ensure_upstream_snapshot", forbid_download)
    result = evaluate_lens_sensor_legacy_isp(
        service.registry.blob_path(record["source_artifacts"]["json"]["sha256"]),
        service.registry.blob_path(record["source_artifacts"]["raw"]["sha256"]),
        source_lens_sensor_job_id=saved["id"], sensor_db_root=ROOT,
        workspace=WORKSPACE, demosaic_method="bilinear",
    )
    assert result["summary"]["raw_values_byte_equal"] is True
    assert result["arrays"]["srgb"].shape == (16, 16, 3)


def test_scene_claim_and_readout_drift_are_rejected_before_isp(source, tmp_path):
    from camera_platform.lens_sensor_legacy_isp import preflight_lens_sensor_legacy_isp
    from camera_platform.retained_raw_dataset_job import _lens_source

    service, saved = source
    source_record = _lens_source(service, {"source_job_id": saved["id"]})
    report_path = service.registry.blob_path(source_record["source_artifacts"]["json"]["sha256"])
    raw_path = service.registry.blob_path(source_record["source_artifacts"]["raw"]["sha256"])
    original = json.loads(report_path.read_bytes())
    for label, mutate, expected in (
        (
            "scene", lambda report: report["preview"]["scene"].update(kind="unknown"),
            "explicit D65 scene",
        ),
        (
            "readout", lambda report: report["preview"]["sensor_model"][
                "effective_readout"
            ].update(black_level_dn=0), "Retained RAW readout differs"),
    ):
        altered = copy.deepcopy(original)
        mutate(altered)
        changed = tmp_path / f"{label}.json"
        changed.write_text(json.dumps(altered, sort_keys=True, separators=(",", ":")) + "\n")
        with pytest.raises(ValueError, match=expected):
            preflight_lens_sensor_legacy_isp(
                changed, raw_path,
                source_lens_sensor_job_id=saved["id"],
                sensor_db_root=ROOT, workspace=WORKSPACE,
                demosaic_method="adaptive_laplacian",
            )
