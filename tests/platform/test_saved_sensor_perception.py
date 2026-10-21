"""One-image task metrics require pinned image, labels, model and predictions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.saved_sensor_perception import (
    ANNOTATION_SCHEMA,
    COORDINATES,
    MODEL_SCHEMA,
    PREDICTION_SCHEMA,
    QUALITY_SCHEMA,
    default_spec,
    evaluate_saved_sensor_predictions,
    get,
    install_routes,
    prepare,
    saved_quality_image_binding,
)
from camera_platform.saved_sensor_quality_job import (
    default_spec as quality_default_spec,
)
from camera_platform.saved_sensor_quality_job import (
    execute as execute_quality,
)
from camera_platform.saved_sensor_quality_job import (
    submit as submit_quality,
)
from camera_platform.sensor_db_bridge import sensor_revision_catalog
from camera_platform.service import PlatformService

WORKSPACE = Path(__file__).resolve().parents[2]


def _write_json(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _document_sha(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _import_document(transport: TestClient, kind: str, document: dict) -> dict:
    response = transport.post(
        "/api/v1/saved-sensor-perception/artifacts",
        json={"kind": kind, "document": document, "expected_sha256": _document_sha(document)},
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["artifact"]["sha256"] == _document_sha(document)
    assert result["model_inference_performed"] is False
    assert result["model_weight_import_supported"] is False
    return result["artifact"]


def _inputs(tmp_path: Path) -> dict:
    image = np.zeros((16, 16, 3), dtype=np.float32)
    image[3:10, 3:10] = 0.5
    npz = tmp_path / "quality.npz"
    np.savez_compressed(npz, case_000_rgb_frame=image)
    quality = _write_json(
        tmp_path / "quality.json",
        {
            "schema": QUALITY_SCHEMA,
            "status": "completed",
            "revision": {"run_id": "saved-run-1"},
            "artifacts": {"npz_sha256": _sha(npz)},
            "controls": {"output_size": [16, 16]},
            "cases": [{"scene": "slanted_edge", "array_prefix": "case_000"}],
        },
    )
    binding = saved_quality_image_binding(
        quality_report_path=quality,
        quality_npz_path=npz,
        quality_job_id="quality-job-1",
        sensor_run_id="saved-run-1",
        scene="slanted_edge",
    )
    label_set = {"id": "road-users", "version": "1.0", "labels": ["person"]}
    model_blob = tmp_path / "model.bin"
    model_blob.write_bytes(b"versioned local model artifact bytes")
    manifest = _write_json(
        tmp_path / "model.json",
        {
            "schema": MODEL_SCHEMA,
            "id": "detector-x",
            "version": "2026.09.28",
            "backend": "ultralytics_yolo",
            "checkpoint_sha256": _sha(model_blob),
            "label_set": label_set,
            "preprocessing": {"color_space": "linear_rgb", "resize": "none"},
        },
    )
    annotations = _write_json(
        tmp_path / "annotations.json",
        {
            "schema": ANNOTATION_SCHEMA,
            "image_binding": binding,
            "annotation_set": {"id": "engineer-labels", "version": "v2"},
            "label_set": label_set,
            "objects": [{"bbox": [3, 3, 10, 10], "label": "person"}],
        },
    )
    predictions = _write_json(
        tmp_path / "predictions.json",
        {
            "schema": PREDICTION_SCHEMA,
            "image_binding": binding,
            "label_set": label_set,
            "model_manifest_sha256": _sha(manifest),
            "model_artifact_sha256": _sha(model_blob),
            "inference_run_id": "external-inference-1",
            "detections": [
                {"bbox": [3, 3, 10, 10], "label": "person", "score": 0.9},
                {"bbox": [11, 11, 15, 15], "label": "person", "score": 0.4},
            ],
        },
    )
    return {
        "quality_report_path": quality,
        "quality_npz_path": npz,
        "annotation_path": annotations,
        "prediction_path": predictions,
        "model_manifest_path": manifest,
        "model_artifact_path": model_blob,
        "quality_job_id": "quality-job-1",
        "sensor_run_id": "saved-run-1",
        "scene": "slanted_edge",
    }


def test_saved_rgb_prediction_artifacts_compute_only_supported_single_image_metrics(tmp_path):
    paths = _inputs(tmp_path)
    result = evaluate_saved_sensor_predictions(**paths)
    assert result["evaluation_kind"] == "supplied_prediction_artifact_single_image"
    assert result["image_binding"]["coordinate_system"] == COORDINATES
    assert result["image_binding"]["array_key"] == "case_000_rgb_frame"
    assert result["annotation_provenance"]["version"] == "v2"
    assert result["model_provenance"]["checkpoint_sha256"] == _sha(paths["model_artifact_path"])
    assert result["prediction_provenance"]["model_execution_verified"] is False
    assert result["model_provenance"]["runtime_extension"]["this_job_inference_performed"] is False
    metrics = result["metrics"]
    assert metrics["true_positive"] == 1
    assert metrics["false_positive"] == 1
    assert metrics["false_negative"] == 0
    assert metrics["precision"] == 0.5
    assert metrics["recall"] == 1.0
    assert metrics["f1"] == pytest.approx(2 / 3)
    assert metrics["dataset_map"]["available"] is False
    assert "map" not in metrics and "average_precision" not in metrics
    assert "average_precision" not in metrics["per_label"]["person"]


def test_image_coordinate_binding_rejects_another_saved_frame(tmp_path):
    paths = _inputs(tmp_path)
    prediction = json.loads(paths["prediction_path"].read_text())
    prediction["image_binding"]["array_sha256"] = "0" * 64
    _write_json(paths["prediction_path"], prediction)
    with pytest.raises(ValueError, match="not bound"):
        evaluate_saved_sensor_predictions(**paths)


def test_model_and_label_identity_must_match_prediction_and_annotations(tmp_path):
    paths = _inputs(tmp_path)
    paths["model_artifact_path"].write_bytes(b"different model bytes")
    with pytest.raises(ValueError, match="Model artifact differs"):
        evaluate_saved_sensor_predictions(**paths)
    paths = _inputs(tmp_path)
    prediction = json.loads(paths["prediction_path"].read_text())
    prediction["label_set"]["version"] = "v3"
    _write_json(paths["prediction_path"], prediction)
    with pytest.raises(ValueError, match="label-set identity"):
        evaluate_saved_sensor_predictions(**paths)


def test_native_or_out_of_frame_boxes_are_rejected(tmp_path):
    paths = _inputs(tmp_path)
    annotations = json.loads(paths["annotation_path"].read_text())
    annotations["objects"][0]["bbox"] = [3, 3, 100, 100]
    _write_json(paths["annotation_path"], annotations)
    with pytest.raises(ValueError, match="exceeds the saved output image"):
        evaluate_saved_sensor_predictions(**paths)


def test_rgb_source_requires_matching_saved_quality_npz_hash_and_array(tmp_path):
    paths = _inputs(tmp_path)
    with np.load(paths["quality_npz_path"], allow_pickle=False) as arrays:
        replacement = np.asarray(arrays["case_000_rgb_frame"]).copy()
    replacement[0, 0] = 1
    np.savez_compressed(paths["quality_npz_path"], case_000_rgb_frame=replacement)
    with pytest.raises(ValueError, match="NPZ differs"):
        evaluate_saved_sensor_predictions(**paths)


def test_declared_model_identity_is_not_misreported_as_verified_bytes(tmp_path):
    paths = _inputs(tmp_path)
    paths["model_artifact_path"].unlink()
    paths["model_artifact_path"] = None
    result = evaluate_saved_sensor_predictions(**paths)
    assert result["model_provenance"]["checkpoint_bytes_verified"] is False
    assert "user-declared" in result["limitations"][0]
    assert result["prediction_provenance"]["model_execution_verified"] is False


def test_import_requires_declared_canonical_hash_and_bounded_json(tmp_path):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    app = FastAPI()
    install_routes(app, service)
    document = {"schema": MODEL_SCHEMA, "id": "small-manifest"}
    with TestClient(app) as transport:
        bad = transport.post(
            "/api/v1/saved-sensor-perception/artifacts",
            json={"kind": "model_manifest", "document": document, "expected_sha256": "0" * 64},
        )
        assert bad.status_code == 422
        assert not any(service.registry.blobs.iterdir())
        artifact = _import_document(transport, "model_manifest", document)
        assert (
            service.registry.blob_path(artifact["sha256"]).read_bytes()
            == json.dumps(
                document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        )
        oversized = {
            "schema": MODEL_SCHEMA,
            "id": "manifest-oversized",
            "padding": "a" * (256 * 1024),
        }
        rejected = transport.post(
            "/api/v1/saved-sensor-perception/artifacts",
            json={
                "kind": "model_manifest",
                "document": oversized,
                "expected_sha256": _document_sha(oversized),
            },
        )
        assert rejected.status_code == 413


def test_pinned_saved_quality_job_accepts_user_evidence_and_preserves_result(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Saved sensor simulation DB is unavailable")
    choices = sensor_revision_catalog(root=WORKSPACE / "camerae2e_db", query="DEF-1801-802")
    selected = next((item for item in choices if item["report_code"] == "DEF-1801-802"), None)
    if selected is None:
        pytest.skip("Reference saved sensor run is unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    service = app.state.service
    quality_spec = quality_default_spec()
    quality_spec.update(
        sensor_revisions=[{"identifier": "DEF-1801-802", "run_id": selected["run_id"]}],
        scenes=["uniform"],
        frames=2,
        output_size=[16, 16],
        acknowledge_simulation_limits=True,
    )
    quality_job = submit_quality(service, quality_spec)
    assert service.registry.claim()["id"] == quality_job["id"]
    quality_result = execute_quality(service, quality_job)
    service.registry.finish(quality_job["id"], result=quality_result)
    report = quality_result["summary"]["sensor_reports"][0]
    binding = saved_quality_image_binding(
        quality_report_path=service.registry.blob_path(report["artifacts"]["json"]["sha256"]),
        quality_npz_path=service.registry.blob_path(report["artifacts"]["npz"]["sha256"]),
        quality_job_id=quality_job["id"],
        sensor_run_id=selected["run_id"],
        scene="uniform",
    )
    labels = {"id": "objects", "version": "1", "labels": ["target"]}
    declared_model_sha = hashlib.sha256(b"model-bytes-held-outside-platform").hexdigest()
    manifest_document = {
        "schema": MODEL_SCHEMA,
        "id": "model-a",
        "version": "1",
        "backend": "external_predictions",
        "checkpoint_sha256": declared_model_sha,
        "label_set": labels,
        "preprocessing": {"rgb": "saved_fixed_linear"},
    }
    annotation_document = {
        "schema": ANNOTATION_SCHEMA,
        "image_binding": binding,
        "annotation_set": {"id": "manual", "version": "1"},
        "label_set": labels,
        "objects": [{"bbox": [2, 2, 6, 6], "label": "target"}],
    }
    prediction_document = {
        "schema": PREDICTION_SCHEMA,
        "image_binding": binding,
        "label_set": labels,
        "model_manifest_sha256": _document_sha(manifest_document),
        "model_artifact_sha256": declared_model_sha,
        "inference_run_id": "external-1",
        "detections": [{"bbox": [2, 2, 6, 6], "label": "target", "score": 0.9}],
    }
    with TestClient(app) as transport:
        transport.headers.update(
            {"x-platform-token": transport.get("/api/v1/session").json()["token"]}
        )
        manifest = _import_document(transport, "model_manifest", manifest_document)
        annotations = _import_document(transport, "annotation", annotation_document)
        predictions = _import_document(transport, "prediction", prediction_document)
    spec = default_spec()
    spec.update(
        quality_job_id=quality_job["id"],
        sensor_run_id=selected["run_id"],
        scene="uniform",
        annotation_artifact_sha256=annotations["sha256"],
        prediction_artifact_sha256=predictions["sha256"],
        model_manifest_sha256=manifest["sha256"],
        model_artifact_sha256="",
        acknowledge_external_predictions=True,
    )
    frozen = prepare(service, spec)
    assert frozen["source_identities"]["image_binding"] == binding
    with TestClient(app) as transport:
        transport.headers.update(
            {"x-platform-token": transport.get("/api/v1/session").json()["token"]}
        )
        preview = transport.get(
            "/api/v1/saved-sensor-perception/template",
            params={
                "quality_job_id": quality_job["id"],
                "sensor_run_id": selected["run_id"],
                "scene": "uniform",
            },
        )
        assert preview.status_code == 200
        assert preview.json()["image_binding"] == binding
        submitted = transport.post("/api/v1/saved-sensor-perception", json=spec)
        assert submitted.status_code == 202
        job = submitted.json()
        pending = transport.get(
            f"/api/v1/jobs/{job['id']}", params={"expected_kind": "saved_sensor_perception"}
        )
        assert pending.status_code == 200
        assert pending.json()["status"] == "queued"
    assert service.registry.claim()["id"] == job["id"]
    result = service.execute_run(job)
    service.registry.finish(job["id"], result=result)
    with TestClient(app) as transport:
        status = transport.get(
            f"/api/v1/jobs/{job['id']}", params={"expected_kind": "saved_sensor_perception"}
        )
        assert status.status_code == 200
        assert status.json()["status"] == "succeeded"
        assert status.json()["detail_path"] == f"saved-sensor-perception/{job['id']}"
        polled = transport.get(f"/api/v1/{status.json()['detail_path']}")
        assert polled.status_code == 200
        completed = polled.json()
    assert get(service, job["id"])["status"] == "succeeded"
    evaluation = completed["result"]["summary"]["evaluation"]
    assert evaluation["metrics"]["true_positive"] == 1
    assert evaluation["metrics"]["dataset_map"]["available"] is False
    assert evaluation["prediction_provenance"]["model_execution_verified"] is False
    assert evaluation["model_provenance"]["checkpoint_bytes_verified"] is False
    assert completed["artifacts"][0]["name"] == "saved-sensor-prediction-result.json"
