"""Provided boxes stay bound to one exact simulated Lens RAW and ISP RGB."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest

from camera_platform import isp_v2
from camera_platform.lens_catalog_bridge import lens_revision_catalog
from camera_platform.lens_sensor_calibrated_isp import calibration_binding
from camera_platform.lens_sensor_calibrated_isp_job import (
    default_spec as calibrated_default_spec,
)
from camera_platform.lens_sensor_calibrated_isp_job import submit as submit_calibrated
from camera_platform.lens_sensor_isp_perception import (
    COORDINATES,
    _npz_array,
    evaluate_lens_sensor_isp_predictions,
    lens_sensor_isp_image_binding,
)
from camera_platform.lens_sensor_isp_perception_cohort import _verified_case
from camera_platform.lens_sensor_isp_perception_job import (
    default_spec as perception_default_spec,
)
from camera_platform.lens_sensor_isp_perception_job import get as get_perception
from camera_platform.lens_sensor_isp_perception_job import submit as submit_perception
from camera_platform.lens_sensor_isp_replay_job import (
    default_spec as isp_default_spec,
)
from camera_platform.lens_sensor_isp_replay_job import submit as submit_isp
from camera_platform.lens_sensor_raw_job import default_spec as raw_default_spec
from camera_platform.lens_sensor_raw_job import submit as submit_raw
from camera_platform.retained_raw_isp_bridge import inspect_lens_sensor_isp_input
from camera_platform.sensor_db_bridge import sensor_revision_catalog
from camera_platform.service import PlatformService
from camera_platform.worker import execute

WORKSPACE = Path(__file__).resolve().parents[2]


def _write(path, document):
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def completed_jobs(tmp_path_factory):
    db_root = WORKSPACE / "camerae2e_db"
    if not (db_root / "catalog.sqlite").is_file():
        pytest.skip("Pinned Lens and Sensor DB are unavailable")
    lens = next(
        (row for row in lens_revision_catalog(catalog_root=db_root)["revisions"]
         if row["preview_status"] == "ready"), None,
    )
    sensor = next(
        (row for row in sensor_revision_catalog(root=db_root, query="DEF-2512-801")
         if row["report_code"] == "DEF-2512-801"), None,
    )
    if lens is None or sensor is None:
        pytest.skip("Pinned Lens or real 2x2 Bayer sensor revision unavailable")
    service = PlatformService(tmp_path_factory.mktemp("lens-isp-perception-state"), WORKSPACE)
    raw_spec = raw_default_spec()
    raw_spec.update(
        lens_revision_id=lens["revision_id"],
        sensor_revision={"identifier": sensor["report_code"], "run_id": sensor["run_id"]},
        scene_kind="uniform", sensor_size=[8, 8], noise_flag=2,
        accept_approximate_lens_radiometry=True,
    )
    raw_queued = submit_raw(service, raw_spec)
    assert service.registry.claim()["id"] == raw_queued["id"]
    execute(service, raw_queued["id"])
    raw_job = service.registry.get_run(raw_queued["id"])
    assert raw_job["status"] == "succeeded", raw_job.get("error")
    isp_spec = isp_default_spec()
    isp_spec.update(
        source_lens_sensor_job_id=raw_job["id"],
        acknowledge_simulation_limits=True,
    )
    isp_queued = submit_isp(service, isp_spec)
    assert service.registry.claim()["id"] == isp_queued["id"]
    execute(service, isp_queued["id"])
    isp_job = service.registry.get_run(isp_queued["id"])
    assert isp_job["status"] == "succeeded", isp_job.get("error")
    raw_artifacts = {row["name"]: row for row in raw_job["artifacts"]}
    isp_artifacts = {row["name"]: row for row in isp_job["artifacts"]}
    paths = {
        "lens_raw_job": raw_job,
        "lens_raw_report_path": service.registry.blob_path(
            raw_artifacts["lens-sensor-raw-preview-result.json"]["sha256"]
        ),
        "lens_raw_path": service.registry.blob_path(
            raw_artifacts["lens-sensor-original-raw.npy"]["sha256"]
        ),
        "lens_isp_job": isp_job,
        "lens_isp_result_path": service.registry.blob_path(
            isp_artifacts["lens-sensor-isp-replay-result.json"]["sha256"]
        ),
        "lens_isp_report_path": service.registry.blob_path(
            isp_artifacts["lens-sensor-isp-replay.json"]["sha256"]
        ),
        "lens_isp_npz_path": service.registry.blob_path(
            isp_artifacts["lens-sensor-isp-replay.npz"]["sha256"]
        ),
    }
    return {"paths": paths, "service": service}


@pytest.fixture(scope="module")
def calibrated_job(completed_jobs):
    service = completed_jobs["service"]
    paths = dict(completed_jobs["paths"])
    raw_job = paths["lens_raw_job"]
    checked = inspect_lens_sensor_isp_input(
        paths["lens_raw_report_path"], paths["lens_raw_path"],
        source_lens_sensor_job_id=raw_job["id"],
        sensor_db_root=WORKSPACE / "camerae2e_db",
        profile=isp_v2.default_profile(),
    )
    report = json.loads(paths["lens_raw_report_path"].read_text())
    binding = calibration_binding(report, checked)
    rows, columns = binding["geometry"]["full_sensor_shape"]
    numeric = {
        "schema": "camera_isp_calibration_v1", "sites": ["R", "Gr", "Gb", "B"],
        "lsc": {
            "schema": "native_four_site_grid_v1",
            "row_nodes": [0, rows - 1], "column_nodes": [0, columns - 1],
            "gains": [
                [[1.0, 1.1, 1.1, 1.2], [1.1, 1.2, 1.2, 1.3]],
                [[1.2, 1.3, 1.3, 1.4], [1.3, 1.4, 1.4, 1.5]],
            ],
            "valid_mask": [[[True] * 4 for _ in range(2)] for _ in range(2)],
            "interpolation": "bilinear",
            "normalization": "declared_per_site_reference",
            "reference_roi": {
                "row_start": 0, "row_end": rows,
                "column_start": 0, "column_end": columns,
            },
        },
        "noise": {
            "schema": "native_four_site_noise_v1",
            "slope_dn": [0.5] * 4, "intercept_dn2": [4.0] * 4,
            "signal_interval_dn": [-64, 4000],
            "reference": "black_subtracted_dn_before_lsc",
            "variance_model": "slope_times_max_signal_zero_plus_intercept",
            "spatial_independence": "assumed",
        },
    }
    calibration = {
        "schema": "camerae2e_lens_sensor_isp_calibration_v1",
        "provenance": {
            "source_kind": "simulated",
            "source_uri": "fixture://separate-flat-dark",
            "method_and_version": "authored_numeric_fixture:1",
            "source_dataset_id": "separate-flat-dark-series",
            "source_dataset_sha256": "a" * 64,
            "target_raw_used_for_fitting": False,
            "uncertainty": {"kind": "unknown", "reason": "Fixture is declaration only."},
        },
        "binding": binding, "payload": numeric,
    }
    content = (json.dumps(calibration, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = service.registry.store_blob(
        content, "lens-sensor-isp-calibration.json", "application/json"
    )
    spec = calibrated_default_spec()
    spec.update(
        source_lens_sensor_job_id=raw_job["id"],
        calibration_sha256=descriptor["sha256"],
        acknowledge_simulation_limits=True,
    )
    spec["profile"]["calibration_id"] = "sha256:" + descriptor["sha256"]
    spec["profile"]["spatial_lsc"]["mode"] = "native_four_site_grid_v1"
    spec["profile"]["raw_denoise"].update(
        mode="calibrated_same_site_filter_v1", strength=0.8
    )
    queued = submit_calibrated(service, spec)
    assert service.registry.claim()["id"] == queued["id"]
    execute(service, queued["id"])
    job = service.registry.get_run(queued["id"])
    assert job["status"] == "succeeded", job.get("error")
    by_name = {row["name"]: row for row in job["artifacts"]}
    paths.update(
        lens_isp_job=job,
        lens_isp_result_path=service.registry.blob_path(
            by_name["lens-sensor-calibrated-isp-result.json"]["sha256"]
        ),
        lens_isp_report_path=service.registry.blob_path(
            by_name["lens-sensor-calibrated-isp.json"]["sha256"]
        ),
        lens_isp_npz_path=service.registry.blob_path(
            by_name["lens-sensor-calibrated-isp.npz"]["sha256"]
        ),
    )
    return paths


@pytest.fixture
def evidence(completed_jobs, tmp_path):
    paths = dict(completed_jobs["paths"])
    binding = lens_sensor_isp_image_binding(**paths)
    label_set = {"id": "test-labels", "version": "1", "labels": ["object"]}
    model_blob = tmp_path / "model.bin"
    model_blob.write_bytes(b"declared separate detector artifact")
    model_sha = hashlib.sha256(model_blob.read_bytes()).hexdigest()
    manifest = tmp_path / "model.json"
    manifest_sha = _write(manifest, {
        "schema": "camerae2e_detection_model_manifest_v1",
        "id": "external-model", "version": "2026.09.28",
        "backend": "external_predictions", "checkpoint_sha256": model_sha,
        "label_set": label_set,
        "preprocessing": {
            "input_color_encoding": binding["color_encoding"],
            "input_shape": binding["shape"],
            "input_dtype": binding["dtype"],
            "channel_order": "RGB", "resize": "none",
        },
    })
    annotations = tmp_path / "annotations.json"
    _write(annotations, {
        "schema": "camerae2e_detection_annotations_v1",
        "image_binding": binding,
        "annotation_set": {"id": "synthetic-test-labels", "version": "1"},
        "ground_truth_provenance": {
            "source_kind": "human_annotation",
            "source_uri": "fixture://independent-test-labels",
            "source_dataset_id": "separate-label-fixture",
            "source_dataset_sha256": "a" * 64,
            "method_and_version": "test-fixture:1",
            "predictions_used_for_annotation": False,
            "uncertainty": {"kind": "unknown", "reason": "Fixture labels are not validated."},
        },
        "label_set": label_set,
        "objects": [{"bbox": [1, 1, 4, 4], "label": "object"}],
    })
    predictions = tmp_path / "predictions.json"
    _write(predictions, {
        "schema": "camerae2e_detection_predictions_v1",
        "image_binding": binding,
        "label_set": label_set,
        "model_manifest_sha256": manifest_sha,
        "model_artifact_sha256": model_sha,
        "inference_run_id": "external-test-predictions",
        "detections": [
            {"bbox": [1, 1, 4, 4], "label": "object", "score": 0.9},
            {"bbox": [5, 5, 7, 7], "label": "object", "score": 0.4},
        ],
    })
    return {
        **paths,
        "annotation_path": annotations,
        "prediction_path": predictions,
        "model_manifest_path": manifest,
        "model_artifact_path": model_blob,
    }


def test_exact_lens_isp_frame_evaluates_supplied_boxes_only(evidence):
    result = evaluate_lens_sensor_isp_predictions(**evidence)
    binding = result["image_binding"]
    assert binding["lens_raw_job_id"] == evidence["lens_raw_job"]["id"]
    assert binding["lens_isp_job_id"] == evidence["lens_isp_job"]["id"]
    assert binding["coordinate_system"] == COORDINATES
    assert binding["array_key"] == "candidate_rgb"
    assert binding["shape"] == [8, 8, 3]
    assert result["metrics"]["true_positive"] == 1
    assert result["metrics"]["false_positive"] == 1
    assert result["metrics"]["false_negative"] == 0
    assert result["metrics"]["precision"] == 0.5
    assert result["metrics"]["dataset_map"]["available"] is False
    assert "map" not in result["metrics"]
    assert result["prediction_provenance"]["model_execution_verified"] is False
    assert result["verification"]["detector_inference"] == "not_performed"
    assert result["annotation_provenance"]["independence_verification"].startswith(
        "supplier_declared"
    )


def test_synthetic_contract_boxes_are_evaluable_without_claiming_object_truth(evidence):
    document = json.loads(evidence["annotation_path"].read_text())
    document["ground_truth_provenance"]["source_kind"] = "synthetic_contract_fixture"
    document["ground_truth_provenance"]["source_uri"] = (
        "fixture://authored-boxes-without-semantic-object-truth"
    )
    _write(evidence["annotation_path"], document)

    result = evaluate_lens_sensor_isp_predictions(**evidence)
    assert result["metrics"]["true_positive"] == 1
    assert result["metrics"]["dataset_map"]["available"] is False
    assert result["annotation_provenance"]["ground_truth_source"]["source_kind"] == (
        "synthetic_contract_fixture"
    )
    assert result["annotation_provenance"]["independence_verification"] == (
        "semantic_ground_truth_absent_synthetic_contract_fixture"
    )
    assert result["verification"]["ground_truth_independence"] == (
        "semantic_ground_truth_absent_synthetic_contract_fixture"
    )
    assert any("no verified semantic object truth" in text for text in result["limitations"])

    document["ground_truth_provenance"]["predictions_used_for_annotation"] = True
    _write(evidence["annotation_path"], document)
    with pytest.raises(ValueError, match="derived from supplied predictions"):
        evaluate_lens_sensor_isp_predictions(**evidence)


def test_independent_reference_source_keeps_existing_unverified_claim(evidence):
    document = json.loads(evidence["annotation_path"].read_text())
    document["ground_truth_provenance"]["source_kind"] = "independent_reference"
    _write(evidence["annotation_path"], document)

    result = evaluate_lens_sensor_isp_predictions(**evidence)
    assert result["annotation_provenance"]["independence_verification"] == (
        "supplier_declared_not_independently_verified"
    )
    assert result["verification"]["ground_truth_independence"] == (
        "supplier_declared_not_independently_verified"
    )
    assert len(result["limitations"]) == 5


@pytest.mark.parametrize("field", ["array_sha256", "coordinate_system", "lens_isp_job_id"])
def test_prediction_must_name_exact_rgb_frame(evidence, field):
    document = json.loads(evidence["prediction_path"].read_text())
    document["image_binding"][field] = "different-frame"
    _write(evidence["prediction_path"], document)
    with pytest.raises(ValueError, match="exact Lens ISP RGB frame"):
        evaluate_lens_sensor_isp_predictions(**evidence)


def test_source_job_and_isp_job_must_share_exact_raw(evidence):
    changed = dict(evidence)
    changed["lens_isp_job"] = deepcopy(evidence["lens_isp_job"])
    changed["lens_isp_job"]["source_capture_result_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="same exact frame"):
        lens_sensor_isp_image_binding(**{
            key: changed[key] for key in (
                "lens_raw_job", "lens_raw_report_path", "lens_raw_path", "lens_isp_job",
                "lens_isp_result_path", "lens_isp_report_path", "lens_isp_npz_path",
            )
        })


def test_annotation_independence_and_image_coordinates_are_checked(evidence):
    document = json.loads(evidence["annotation_path"].read_text())
    document["ground_truth_provenance"]["predictions_used_for_annotation"] = True
    _write(evidence["annotation_path"], document)
    with pytest.raises(ValueError, match="derived from supplied predictions"):
        evaluate_lens_sensor_isp_predictions(**evidence)
    document["ground_truth_provenance"]["predictions_used_for_annotation"] = False
    document["objects"][0]["bbox"] = [1, 1, 40, 40]
    _write(evidence["annotation_path"], document)
    with pytest.raises(ValueError, match="exceeds the saved output image"):
        evaluate_lens_sensor_isp_predictions(**evidence)


def test_model_preprocessing_must_match_isp_encoding_and_grid(evidence):
    manifest = json.loads(evidence["model_manifest_path"].read_text())
    manifest["preprocessing"]["input_color_encoding"] = "linear_rgb"
    changed_manifest_sha = _write(evidence["model_manifest_path"], manifest)
    predictions = json.loads(evidence["prediction_path"].read_text())
    predictions["model_manifest_sha256"] = changed_manifest_sha
    _write(evidence["prediction_path"], predictions)
    with pytest.raises(ValueError, match="exact ISP RGB encoding and grid"):
        evaluate_lens_sensor_isp_predictions(**evidence)


def test_supplied_model_bytes_and_rgb_npz_must_match_hashes(evidence, tmp_path):
    evidence["model_artifact_path"].write_bytes(b"changed model artifact")
    with pytest.raises(ValueError, match="Model artifact differs"):
        evaluate_lens_sensor_isp_predictions(**evidence)
    changed = dict(evidence)
    changed["model_artifact_path"] = None
    changed["lens_isp_npz_path"] = tmp_path / "changed.npz"
    changed["lens_isp_npz_path"].write_bytes(evidence["lens_isp_npz_path"].read_bytes() + b"x")
    with pytest.raises(ValueError, match="artifact hashes differ"):
        lens_sensor_isp_image_binding(**{
            key: changed[key] for key in (
                "lens_raw_job", "lens_raw_report_path", "lens_raw_path", "lens_isp_job",
                "lens_isp_result_path", "lens_isp_report_path", "lens_isp_npz_path",
            )
        })


@pytest.mark.parametrize(
    "annotation_source_kind", ["human_annotation", "synthetic_contract_fixture"]
)
def test_completed_lens_rgb_supplied_prediction_worker(
    completed_jobs, evidence, annotation_source_kind
):
    service = completed_jobs["service"]
    source_paths = completed_jobs["paths"]
    if annotation_source_kind == "synthetic_contract_fixture":
        annotations = json.loads(evidence["annotation_path"].read_text())
        annotations["ground_truth_provenance"]["source_kind"] = annotation_source_kind
        annotations["ground_truth_provenance"]["source_uri"] = (
            "fixture://authored-boxes-without-semantic-object-truth"
        )
        _write(evidence["annotation_path"], annotations)
    registered = {}
    for key, name in (
        ("annotation_path", "lens-isp-annotations.json"),
        ("prediction_path", "lens-isp-predictions.json"),
        ("model_manifest_path", "lens-isp-model-manifest.json"),
        ("model_artifact_path", "lens-isp-model.bin"),
    ):
        path = evidence[key]
        registered[key] = service.registry.store_blob(
            path.read_bytes(), name,
            "application/octet-stream" if key == "model_artifact_path" else "application/json",
        )
    spec = perception_default_spec()
    spec.update(
        source_lens_sensor_job_id=source_paths["lens_raw_job"]["id"],
        source_lens_sensor_isp_job_id=source_paths["lens_isp_job"]["id"],
        annotation_artifact_sha256=registered["annotation_path"]["sha256"],
        prediction_artifact_sha256=registered["prediction_path"]["sha256"],
        model_manifest_sha256=registered["model_manifest_path"]["sha256"],
        model_artifact_sha256=registered["model_artifact_path"]["sha256"],
        acknowledge_external_predictions=True,
    )
    queued = submit_perception(service, spec)
    assert service.registry.claim()["id"] == queued["id"]
    execute(service, queued["id"])
    finished = get_perception(service, queued["id"])
    assert finished["status"] == "succeeded", finished.get("error")
    result = finished["result"]["summary"]
    assert result["source_identities"]["image_binding"]["lens_isp_job_id"] == (
        source_paths["lens_isp_job"]["id"]
    )
    assert result["evaluation"]["metrics"]["true_positive"] == 1
    assert result["evaluation"]["metrics"]["dataset_map"]["available"] is False
    assert result["evaluation"]["verification"]["detector_inference"] == "not_performed"
    assert (
        result["evaluation"]["annotation_provenance"]["ground_truth_source"]
        ["source_kind"] == annotation_source_kind
    )
    cohort_case = _verified_case(service, finished["id"])
    assert cohort_case["evaluation_job_id"] == finished["id"]
    assert cohort_case["scene_family"] == "uniform_d65"
    assert cohort_case["scene_asset_identity"] is None
    assert cohort_case["annotation_source"]["source_kind"] == annotation_source_kind
    assert len(cohort_case["truth"]) == 1


def test_real_calibrated_isp_rgb_is_bound_as_float64(calibrated_job, evidence):
    paths = dict(calibrated_job)
    binding = lens_sensor_isp_image_binding(**paths)
    assert binding["lens_isp_job_kind"] == "lens_sensor_calibrated_isp"
    assert binding["dtype"] == "float64"
    assert binding["shape"] == [8, 8, 3]
    assert binding["color_encoding"] == "srgb"
    with np.load(paths["lens_isp_npz_path"], allow_pickle=False) as arrays:
        rgb = arrays["candidate_rgb"]
    assert rgb.dtype == np.dtype("float64")
    assert binding["array_sha256"] == hashlib.sha256(rgb.tobytes(order="C")).hexdigest()
    with pytest.raises(ValueError, match="array header differs"):
        _npz_array(paths["lens_isp_npz_path"], "candidate_rgb", (8, 8, 3), "float32")

    annotation = json.loads(evidence["annotation_path"].read_text())
    annotation["image_binding"] = binding
    _write(evidence["annotation_path"], annotation)
    manifest = json.loads(evidence["model_manifest_path"].read_text())
    manifest["preprocessing"]["input_dtype"] = "float64"
    manifest_sha = _write(evidence["model_manifest_path"], manifest)
    predictions = json.loads(evidence["prediction_path"].read_text())
    predictions["image_binding"] = binding
    predictions["model_manifest_sha256"] = manifest_sha
    _write(evidence["prediction_path"], predictions)
    supplied = {
        **paths,
        "annotation_path": evidence["annotation_path"],
        "prediction_path": evidence["prediction_path"],
        "model_manifest_path": evidence["model_manifest_path"],
        "model_artifact_path": evidence["model_artifact_path"],
    }
    result = evaluate_lens_sensor_isp_predictions(**supplied)
    assert result["image_binding"] == binding
    assert result["metrics"]["true_positive"] == 1
    assert result["verification"]["detector_inference"] == "not_performed"

    manifest["preprocessing"]["input_dtype"] = "float32"
    predictions["model_manifest_sha256"] = _write(evidence["model_manifest_path"], manifest)
    _write(evidence["prediction_path"], predictions)
    with pytest.raises(ValueError, match="exact ISP RGB encoding and grid"):
        evaluate_lens_sensor_isp_predictions(**supplied)
