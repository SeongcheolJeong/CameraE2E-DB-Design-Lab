"""Offline detector execution is tied to one retained Lens RAW and ISP RGB."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import socket
import sys
import types
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest
import test_lens_sensor_isp_perception as source_fixture
from fastapi import FastAPI
from fastapi.testclient import TestClient

from camera_platform import _offline_yolo_worker
from camera_platform.lens_sensor_isp_detector import (
    _metrics_for_annotations,
    _validated_output,
    infer_lens_sensor_isp_detector,
    local_model_preflight,
    local_weight_path,
)
from camera_platform.lens_sensor_isp_detector_job import (
    default_spec,
    get,
    install_routes,
    submit,
)
from camera_platform.lens_sensor_isp_detector_qualification import (
    SPLIT_SCHEMA,
    _test_annotation_cohort_issue,
    detector_replay_intake_template,
    qualify_saved_detector_replay,
)
from camera_platform.lens_sensor_isp_perception import lens_sensor_isp_image_binding
from camera_platform.registry import IntegrityError, canonical
from camera_platform.worker import execute


@pytest.fixture(scope="module")
def completed_jobs(tmp_path_factory):
    return source_fixture.completed_jobs.__wrapped__(tmp_path_factory)


@pytest.fixture(autouse=True)
def operator_trust_for_local_model(monkeypatch):
    path = local_weight_path()
    if path.is_file():
        monkeypatch.setenv(
            "CAMERA_PLATFORM_TRUSTED_YOLO_WEIGHTS_SHA256",
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )


def _model_sha() -> str:
    path = local_weight_path()
    if not path.is_file():
        pytest.skip("Local pinned YOLO11n weights are unavailable")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_actual_offline_yolo_inference_uses_exact_saved_isp_rgb(completed_jobs):
    paths = completed_jobs["paths"]
    report = infer_lens_sensor_isp_detector(
        **paths, model_weights_sha256=_model_sha(), target_labels=["person"],
        score_threshold=0.25,
    )
    binding = lens_sensor_isp_image_binding(**paths)
    assert report["image_binding"] == binding
    assert report["model_provenance"]["weights_sha256"] == _model_sha()
    assert report["verification"]["detector_inference"].startswith("performed_by_offline")
    assert report["runtime"]["ultralytics"]
    assert report["model_class_ids"] == {"person": 0}
    assert report["preprocessing"]["numpy_model_input_channel_order"] == "BGR"
    assert report["preprocessing"]["model_input_bgr_uint8_sha256"]
    assert "rgb_to_bgr_numpy" in report["preprocessing"]["contract"]
    assert report["model_provenance"]["operator_trust_is_authenticity_verification"] is False
    assert report["annotation_evaluation"] is None
    assert report["verification"]["product_perception_performance"] == "not_established"
    assert isinstance(report["detections"], list)
    assert report["detection_count"] == len(report["detections"])


def test_durable_worker_retains_source_model_and_actual_inference(completed_jobs):
    service = completed_jobs["service"]
    paths = completed_jobs["paths"]
    spec = default_spec()
    spec.update(
        source_lens_sensor_job_id=paths["lens_raw_job"]["id"],
        source_lens_sensor_isp_job_id=paths["lens_isp_job"]["id"],
        model_weights_sha256=_model_sha(),
        acknowledge_simulation_limits=True,
    )
    queued = submit(service, spec)
    assert service.registry.claim()["id"] == queued["id"]
    execute(service, queued["id"])
    job = get(service, queued["id"])
    assert job["status"] == "succeeded", job.get("error")
    summary = job["result"]["summary"]
    assert summary["inference"]["image_binding"] == job["source_identities"]["image_binding"]
    assert summary["inference"]["model_provenance"]["weights_sha256"] == _model_sha()
    assert summary["verification"]["detector_inference"].startswith("performed_by_offline")
    assert summary["verification"]["dataset_map"] == "not_established"
    assert service.registry.blob_path(job["artifacts"][0]["sha256"]).read_bytes() == canonical(
        summary
    ).encode()
    intake = detector_replay_intake_template(service, [job["id"]])
    audit = qualify_saved_detector_replay(service, intake)
    assert audit["cases"][0]["detector_output_sha256"] == intake["cases"][0][
        "inference_sha256"
    ]
    assert audit["cases"][0]["rgb_array_sha256"] == summary["inference"][
        "image_binding"
    ]["array_sha256"]
    assert audit["product_map"]["available"] is False
    assert {"annotation_missing", "independent_scene_asset_missing"}.issubset(
        audit["product_map"]["blockers"]
    )
    assert audit["diagnostic_map"]["available"] is False

    app = FastAPI()
    install_routes(app, service)
    with TestClient(app) as transport:
        template_response = transport.get(
            "/api/v1/lens-sensor-isp-detectors/replay-qualifications/template",
            params={"detector_job_id": job["id"]},
        )
        assert template_response.status_code == 200
        assert template_response.json() == intake
        response = transport.post(
            "/api/v1/lens-sensor-isp-detectors/replay-qualifications", json=intake
        )
        assert response.status_code == 201
        body = response.json()
        assert body["report"] == audit
        assert service.registry.blob_path(body["artifact"]["sha256"]).read_bytes() == (
            canonical(audit).encode()
        )

    wrong = deepcopy(intake)
    wrong["cases"][0]["image_binding_sha256"] = "0" * 64
    with pytest.raises(IntegrityError, match="image_binding_sha256"):
        qualify_saved_detector_replay(service, wrong)
    wrong = deepcopy(intake)
    wrong["cases"][0]["split"] = "unsupported"
    with pytest.raises(ValueError, match="Replay split"):
        qualify_saved_detector_replay(service, wrong)

    split = {
        "schema": SPLIT_SCHEMA,
        "dataset_id": intake["dataset_id"],
        "dataset_revision": intake["dataset_revision"],
        "source_uri": "fixture://declared-scene-splits",
        "method_and_version": "single-test-scene:1",
        "assignments": [{
            "scene_group_id": intake["cases"][0]["scene_identity"]["scene_group_id"],
            "split": "test",
        }],
    }
    artifact = service.registry.store_blob(
        canonical(split).encode(), "declared-scene-splits.json", "application/json"
    )
    intake["split_assignment_artifact_sha256"] = artifact["sha256"]
    with_split = qualify_saved_detector_replay(service, intake)
    assert with_split["split_assignment_artifact"]["sha256"] == artifact["sha256"]
    assert "split_assignment_artifact_missing" not in with_split["product_map"][
        "blockers"
    ]
    assert with_split["product_map"]["available"] is False


def test_annotation_metric_requires_independent_exact_rgb_binding(completed_jobs, tmp_path):
    binding = lens_sensor_isp_image_binding(**completed_jobs["paths"])
    document = {
        "schema": "camerae2e_detection_annotations_v1",
        "image_binding": deepcopy(binding),
        "annotation_set": {"id": "human-labels", "version": "1"},
        "ground_truth_provenance": {
            "source_kind": "human_annotation",
            "source_uri": "fixture://separately-authored-boxes",
            "source_dataset_id": "separate-label-set",
            "source_dataset_sha256": "a" * 64,
            "method_and_version": "human-label-fixture:1",
            "predictions_used_for_annotation": False,
            "uncertainty": {"kind": "unknown", "reason": "Fixture correctness is unverified."},
        },
        "label_set": {"id": "coco-person", "version": "1", "labels": ["person"]},
        "objects": [{"bbox": [1, 1, 4, 4], "label": "person"}],
    }
    path = tmp_path / "annotations.json"
    path.write_text(json.dumps(document))
    result = _metrics_for_annotations(
        path, binding=binding, detections=[], target_labels=["person"],
        model_sha256=_model_sha(), iou_threshold=0.5,
    )
    assert result["single_frame_metrics"]["false_negative"] == 1
    assert result["single_frame_metrics"]["dataset_map"]["available"] is False
    document["image_binding"]["array_sha256"] = "0" * 64
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="exact saved Lens ISP RGB"):
        _metrics_for_annotations(
            path, binding=binding, detections=[], target_labels=["person"],
            model_sha256=_model_sha(), iou_threshold=0.5,
        )


def test_negative_fixture_counts_false_positives_without_product_truth(completed_jobs, tmp_path):
    binding = lens_sensor_isp_image_binding(**completed_jobs["paths"])
    annotation = {
        "schema": "camerae2e_detection_annotations_v1",
        "image_binding": binding,
        "annotation_set": {"id": "negative-fixture", "version": "1"},
        "ground_truth_provenance": {
            "source_kind": "synthetic_contract_fixture",
            "source_uri": "fixture://declared-empty-scene",
            "source_dataset_id": "negative-scene-contract",
            "source_dataset_sha256": "a" * 64,
            "method_and_version": "empty-box-fixture:1",
            "predictions_used_for_annotation": False,
            "uncertainty": {"kind": "unknown", "reason": "No semantic truth is verified."},
        },
        "label_set": {"id": "coco-person", "version": "1", "labels": ["person"]},
        "objects": [],
    }
    path = tmp_path / "negative-annotation.json"
    path.write_text(canonical(annotation), encoding="utf-8")
    evaluated = _metrics_for_annotations(
        path, binding=binding,
        detections=[{"bbox": [1, 1, 4, 4], "label": "person", "score": 0.9}],
        target_labels=["person"], model_sha256=_model_sha(), iou_threshold=0.5,
    )
    assert evaluated["single_frame_metrics"]["true_positive"] == 0
    assert evaluated["single_frame_metrics"]["false_positive"] == 1
    assert evaluated["single_frame_metrics"]["false_negative"] == 0
    assert evaluated["independence_verification"] == (
        "semantic_ground_truth_absent_synthetic_contract_fixture"
    )
    service = completed_jobs["service"]
    artifact = service.registry.store_blob(
        path.read_bytes(), "negative-annotation.json", "application/json"
    )
    spec = default_spec()
    spec.update(
        source_lens_sensor_job_id=completed_jobs["paths"]["lens_raw_job"]["id"],
        source_lens_sensor_isp_job_id=completed_jobs["paths"]["lens_isp_job"]["id"],
        model_weights_sha256=_model_sha(),
        annotation_artifact_sha256=artifact["sha256"],
        acknowledge_simulation_limits=True,
    )
    queued = submit(service, spec)
    assert service.registry.claim()["id"] == queued["id"]
    execute(service, queued["id"])
    completed = get(service, queued["id"])
    assert completed["status"] == "succeeded", completed.get("error")
    assert completed["result"]["summary"]["inference"]["verification"]["ground_truth"] == (
        "semantic_ground_truth_absent_synthetic_contract_fixture"
    )
    intake = detector_replay_intake_template(service, [completed["id"]])
    report = qualify_saved_detector_replay(service, intake)
    assert report["cases"][0]["ground_truth_box_count"] == 0
    assert report["cases"][0]["annotation_source_kind"] == "synthetic_contract_fixture"
    assert "semantic_object_ground_truth_missing" in report["product_map"]["blockers"]
    assert report["product_map"]["available"] is False

    unannotated = default_spec()
    unannotated.update(
        source_lens_sensor_job_id=spec["source_lens_sensor_job_id"],
        source_lens_sensor_isp_job_id=spec["source_lens_sensor_isp_job_id"],
        model_weights_sha256=_model_sha(),
        acknowledge_simulation_limits=True,
    )
    other = submit(service, unannotated)
    if other["status"] == "queued":
        assert service.registry.claim()["id"] == other["id"]
        execute(service, other["id"])
    other = get(service, other["id"])
    assert other["status"] == "succeeded", other.get("error")
    leaked = detector_replay_intake_template(service, [completed["id"], other["id"]])
    leaked["cases"][0]["split"] = "train"
    with pytest.raises(ValueError, match="scene source appears in multiple"):
        qualify_saved_detector_replay(service, leaked)


def test_diagnostic_map_rejects_mixed_annotation_cohorts():
    case = {
        "label_set": {"id": "person", "version": "1", "labels": ["person"]},
        "annotation_set": {"id": "held-out-labels", "version": "1"},
        "annotation_source": {"source_dataset_id": "held-out-scenes"},
    }
    other = deepcopy(case)
    assert _test_annotation_cohort_issue([case, other]) is None
    other["annotation_set"]["version"] = "2"
    assert _test_annotation_cohort_issue([case, other]) == (
        "Test annotations use different annotation sets."
    )
    other = deepcopy(case)
    other["annotation_source"]["source_dataset_id"] = "other-scenes"
    assert _test_annotation_cohort_issue([case, other]) == (
        "Test annotations come from different source datasets."
    )


def test_untrusted_detector_output_cannot_change_hash_or_escape_frame():
    rgb = bytes(8 * 8 * 3)
    frame_sha = hashlib.sha256(rgb).hexdigest()
    request = {
        "weights_sha256": "a" * 64, "rgb_uint8_sha256": frame_sha,
        "rgb_uint8_base64": base64.b64encode(rgb).decode("ascii"),
        "rgb_shape": [8, 8, 3], "target_labels": ["person"],
    }
    output = {
        "schema": "camerae2e_offline_yolo_result_v1",
        "inference_performed": True,
        "weights_sha256": request["weights_sha256"],
        "rgb_uint8_sha256": request["rgb_uint8_sha256"],
        "model_input_bgr_uint8_sha256": frame_sha,
        "rgb_shape": request["rgb_shape"],
        "target_labels": request["target_labels"],
        "model_class_ids": {"person": 0},
        "detections": [{"bbox": [1, 1, 9, 4], "label": "person", "score": 0.9, "class_id": 0}],
        "runtime": {
            "python": "3.9.6", "torch": "2.8.0", "ultralytics": "8.4.61",
            "numpy": "2.0.2", "executable": "/usr/bin/python3",
        },
        "preprocessing": (
            "round_float_srgb_to_rgb_uint8_then_rgb_to_bgr_numpy_"
            "then_ultralytics_internal_letterbox"
        ),
    }
    with pytest.raises(ValueError, match="saved output image"):
        _validated_output(output, request=request)
    output["detections"] = []
    output["rgb_uint8_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="pinned image/model contract"):
        _validated_output(output, request=request)
    output["rgb_uint8_sha256"] = request["rgb_uint8_sha256"]
    output["model_input_bgr_uint8_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="RGB-to-BGR conversion"):
        _validated_output(output, request=request)


def test_yolo_numpy_call_receives_bgr_not_original_rgb(monkeypatch, tmp_path):
    captured = {}

    class FakeModel:
        names = {0: "person"}

        def __init__(self, _path):
            pass

        def __call__(self, image, **_kwargs):
            captured["image"] = image.copy()
            return [types.SimpleNamespace(orig_shape=image.shape[:2], boxes=None)]

    fake_ultralytics = types.ModuleType("ultralytics")
    fake_ultralytics.YOLO = FakeModel
    fake_ultralytics.__version__ = "test-double"
    fake_torch = types.ModuleType("torch")
    fake_torch.__version__ = "test-double"
    monkeypatch.setitem(sys.modules, "ultralytics", fake_ultralytics)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    # The worker changes these global socket methods; record originals for undo.
    monkeypatch.setattr(socket, "create_connection", socket.create_connection)
    monkeypatch.setattr(socket.socket, "connect", socket.socket.connect)
    monkeypatch.setattr(socket.socket, "connect_ex", socket.socket.connect_ex)
    weight = tmp_path / "yolo11n.pt"
    weight.write_bytes(b"test-only-model-bytes")
    model_sha = hashlib.sha256(weight.read_bytes()).hexdigest()
    monkeypatch.setenv("CAMERA_PLATFORM_TRUSTED_YOLO_WEIGHTS_SHA256", model_sha)
    monkeypatch.setenv("YOLO_OFFLINE", "true")
    monkeypatch.setenv("YOLO_AUTOINSTALL", "false")
    rgb = np.zeros((8, 8, 3), dtype=np.uint8)
    rgb[0, 0] = [255, 0, 0]  # pure red in the platform's RGB contract
    rgb[0, 1] = [0, 0, 255]  # pure blue
    rgb_bytes = rgb.tobytes(order="C")
    request = {
        "schema": "camerae2e_offline_yolo_request_v1",
        "weights_path": str(weight),
        "weights_sha256": model_sha,
        "rgb_shape": [8, 8, 3],
        "rgb_uint8_base64": base64.b64encode(rgb_bytes).decode("ascii"),
        "rgb_uint8_sha256": hashlib.sha256(rgb_bytes).hexdigest(),
        "score_threshold": 0.25,
        "target_labels": ["person"],
    }
    protocol, diagnostic = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(sys, "stdout", protocol)
    monkeypatch.setattr(sys, "stderr", diagnostic)
    _offline_yolo_worker.main()
    sent = captured["image"]
    assert sent.dtype == np.uint8 and sent.shape == rgb.shape
    assert sent.flags.c_contiguous
    assert sent[0, 0].tolist() == [0, 0, 255]
    assert sent[0, 1].tolist() == [255, 0, 0]
    result = json.loads(protocol.getvalue())
    assert result["model_input_bgr_uint8_sha256"] == hashlib.sha256(
        sent.tobytes(order="C")
    ).hexdigest()
    assert "rgb_to_bgr_numpy" in result["preprocessing"]


def test_untrusted_local_weight_cannot_enter_model_preflight(monkeypatch):
    monkeypatch.delenv("CAMERA_PLATFORM_TRUSTED_YOLO_WEIGHTS_SHA256", raising=False)
    with pytest.raises(ValueError, match="CAMERA_PLATFORM_TRUSTED_YOLO_WEIGHTS_SHA256"):
        local_model_preflight(_model_sha())


def test_template_does_not_auto_trust_cached_pickle_weights(monkeypatch):
    app = FastAPI()
    service = SimpleNamespace(registry=SimpleNamespace(list_runs=lambda: []))
    install_routes(app, service)
    client = TestClient(app)
    monkeypatch.delenv("CAMERA_PLATFORM_TRUSTED_YOLO_WEIGHTS_SHA256", raising=False)
    response = client.get("/api/v1/lens-sensor-isp-detectors/template")
    assert response.status_code == 200
    assert response.json()["specification"]["model_weights_sha256"] == ""
    assert response.json()["availability"]["status"] == "operator_trust_required"
    monkeypatch.setenv("CAMERA_PLATFORM_TRUSTED_YOLO_WEIGHTS_SHA256", _model_sha())
    ready = client.get("/api/v1/lens-sensor-isp-detectors/template")
    assert ready.status_code == 200
    assert ready.json()["availability"]["status"] == "ready_for_development_inference"
    assert ready.json()["specification"]["model_weights_sha256"] == _model_sha()
