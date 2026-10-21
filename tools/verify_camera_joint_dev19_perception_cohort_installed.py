#!/usr/bin/env python3
"""Installed-wheel proof: real Lens RAW/ISP jobs to a diagnostic prediction cohort.

Boxes are deliberately authored contract fixtures on tiny synthetic patterns.
No detector runs, no object-label correctness is established, and the reported
image-aware mAP must never be presented as product perception accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from verify_camera_bounded_psf_installed import _artifact, _client, _verify_wheel

from camera_platform.api import create_app
from camera_platform.registry import canonical
from pyisetcam.dataset import _sha256_array

LENS_REVISION_ID = "6c48e967-74b4-5901-8186-ba6fafe4797b"
SENSOR_REPORT_CODE = "DEF-2512-801"
SENSOR_SIZE = [8, 8]
SCENE_KINDS = ("uniform", "slanted_edge")
SCENE_FAMILIES = ("uniform_d65", "slanted_edge_d65")
AUTHOR_BOXES = {
    "uniform": [{"bbox": [1, 1, 4, 4], "label": "contract_target"}],
    "slanted_edge": [{"bbox": [2, 2, 5, 5], "label": "contract_target"}],
}
AUTHOR_PREDICTIONS = {
    "uniform": [
        {"bbox": [2, 2, 5, 5], "label": "contract_target", "score": 0.95},
        {"bbox": [1, 1, 4, 4], "label": "contract_target", "score": 0.60},
    ],
    "slanted_edge": [
        {"bbox": [2, 2, 5, 5], "label": "contract_target", "score": 0.80},
    ],
}


def _finished(client, queued: dict, kind: str) -> tuple[dict, float]:
    started = time.monotonic()
    job = client.wait(queued["id"], timeout=240, expected_kind=kind)
    elapsed = time.monotonic() - started
    if job["status"] != "succeeded":
        raise RuntimeError(f"Installed {kind} failed: {job.get('error')}")
    if job.get("job_kind") != kind or not job.get("artifacts"):
        raise RuntimeError(f"Installed {kind} lost its durable result.")
    return job, elapsed


def _source_jobs(client, transport: TestClient) -> tuple[list[dict], dict]:
    template = client.lens_sensor_raw_template(scope="central_bounded")
    if template["specification"]["schema"] != "camerae2e_lens_sensor_raw_preview_study_v3":
        raise RuntimeError("Installed strict PSF Lens RAW template is unavailable.")
    if not any(
        row["revision_id"] == LENS_REVISION_ID and row["preview_status"] == "ready"
        for row in template["lens_catalog"]["revisions"]
    ):
        raise RuntimeError("Pinned ready Lens DB PSF revision is unavailable.")
    sensor = next(
        (row for row in template["sensor_catalog"]
         if row["report_code"] == SENSOR_REPORT_CODE and row["model"] == "db_estimate_v1"),
        None,
    )
    if sensor is None:
        raise RuntimeError("Pinned executable saved Sensor DB revision is unavailable.")
    cases = []
    for kind in SCENE_KINDS:
        raw_spec = dict(template["specification"])
        raw_spec.update(
            name=f"dev19 diagnostic-cohort {kind} Lens RAW",
            lens_revision_id=LENS_REVISION_ID,
            sensor_revision={
                "identifier": sensor["report_code"], "run_id": sensor["run_id"],
            },
            scene_kind=kind,
            sensor_size=SENSOR_SIZE.copy(),
            luminance_cd_m2=100.0,
            exposure_s=0.004,
            noise_flag=0,
            seed=7,
            accept_approximate_lens_radiometry=True,
        )
        raw, raw_seconds = _finished(
            client, client.submit_lens_sensor_raw(raw_spec), "lens_sensor_raw"
        )
        preview = client.lens_sensor_raw(raw["id"])["result"]["summary"]["preview"]
        content = _artifact(transport, preview["raw_artifact"])
        image = np.load(io.BytesIO(content), allow_pickle=False)
        if (
            image.dtype != np.dtype("int32")
            or list(image.shape) != SENSOR_SIZE
            or image.tolist() != preview["raw"]["values"]
            or preview["lens_model"]["psf_execution"]["mode"]
            != "strict_bounded_streaming"
            or preview["lens_model"]["psf_execution"]["convolution_strategy"]
            != "cropped_input_full_fft_same_output"
            or preview["scene"]["kind"] != f"{kind}_d65"
        ):
            raise RuntimeError(f"Installed {kind} RAW lacks exact PSF/scene evidence.")
        isp_spec = client.lens_sensor_isp_replay_template()["specification"]
        isp_spec.update(
            name=f"dev19 diagnostic-cohort {kind} saved RAW ISP",
            source_lens_sensor_job_id=raw["id"],
            acknowledge_simulation_limits=True,
        )
        isp, isp_seconds = _finished(
            client, client.submit_lens_sensor_isp_replay(isp_spec),
            "lens_sensor_isp_replay",
        )
        isp_summary = client.lens_sensor_isp_replay(isp["id"])["result"]["summary"]
        if (
            isp_summary["raw_values_byte_equal"] is not True
            or isp_summary["source_capture_result_sha256"]
            != isp["source_capture_result_sha256"]
        ):
            raise RuntimeError(f"Installed {kind} ISP lost its exact RAW binding.")
        binding = client.lens_sensor_isp_perception_template(
            source_lens_sensor_job_id=raw["id"],
            source_lens_sensor_isp_job_id=isp["id"],
        )["image_binding"]
        if (
            binding["shape"] != [*SENSOR_SIZE, 3]
            or binding["lens_raw_job_id"] != raw["id"]
            or binding["lens_isp_job_id"] != isp["id"]
            or binding["source_raw_content_sha256"] != _sha256_array(image)
        ):
            raise RuntimeError(f"Installed {kind} RGB does not bind to the source jobs.")
        cases.append({
            "scene_kind": kind,
            "raw_job_id": raw["id"],
            "isp_job_id": isp["id"],
            "raw_artifact_sha256": preview["raw_artifact"]["sha256"],
            "raw_content_sha256": binding["source_raw_content_sha256"],
            "rgb_array_sha256": binding["array_sha256"],
            "image_binding": binding,
            "raw_elapsed_s": raw_seconds,
            "isp_elapsed_s": isp_seconds,
        })
    if (
        len({case["raw_content_sha256"] for case in cases}) != 2
        or len({case["rgb_array_sha256"] for case in cases}) != 2
    ):
        raise RuntimeError("Two synthetic scene families did not produce distinct RAW/RGB.")
    return cases, sensor


def _evaluation_jobs(client, cases: list[dict]) -> list[dict]:
    label_set = {
        "id": "synthetic-contract-only", "version": "1", "labels": ["contract_target"],
    }
    reference_bytes = canonical(AUTHOR_BOXES).encode("utf-8")
    reference_sha = hashlib.sha256(reference_bytes).hexdigest()
    placeholder_sha = hashlib.sha256(
        b"No detector was run; this is a supplied-prediction wiring fixture."
    ).hexdigest()
    first = cases[0]["image_binding"]
    model_manifest = {
        "schema": "camerae2e_detection_model_manifest_v1",
        "id": "no-detector-executed-contract-fixture",
        "version": "1",
        "backend": "external_predictions",
        "checkpoint_sha256": placeholder_sha,
        "label_set": label_set,
        "preprocessing": {
            "input_color_encoding": first["color_encoding"],
            "input_shape": first["shape"],
            "input_dtype": first["dtype"],
            "channel_order": "RGB",
            "resize": "none",
        },
    }
    manifest = client.import_lens_sensor_isp_perception_artifact(
        "model_manifest", model_manifest
    )["artifact"]
    evaluated = []
    for case in cases:
        kind = case["scene_kind"]
        binding = case["image_binding"]
        for key, expected in (
            ("color_encoding", first["color_encoding"]),
            ("shape", first["shape"]),
            ("dtype", first["dtype"]),
        ):
            if binding[key] != expected:
                raise RuntimeError("Cohort frames have incompatible declared model input.")
        annotations = {
            "schema": "camerae2e_detection_annotations_v1",
            "image_binding": binding,
            "annotation_set": {"id": "authored-contract-boxes", "version": "1"},
            "ground_truth_provenance": {
                "source_kind": "synthetic_contract_fixture",
                "source_uri": "fixture://authored-boxes-no-semantic-object-truth",
                "source_dataset_id": "authored-box-contract-v1",
                "source_dataset_sha256": reference_sha,
                "method_and_version": "fixed-before-predictions:1",
                "predictions_used_for_annotation": False,
                "uncertainty": {
                    "kind": "unknown",
                    "reason": (
                        "Authored boxes only test image binding and score plumbing; "
                        "these patterns have no verified objects."
                    ),
                },
            },
            "label_set": label_set,
            "objects": AUTHOR_BOXES[kind],
        }
        predictions = {
            "schema": "camerae2e_detection_predictions_v1",
            "image_binding": binding,
            "label_set": label_set,
            "model_manifest_sha256": manifest["sha256"],
            "model_artifact_sha256": placeholder_sha,
            "inference_run_id": f"no-inference-authored-{kind}",
            "detections": AUTHOR_PREDICTIONS[kind],
        }
        annotation_artifact = client.import_lens_sensor_isp_perception_artifact(
            "annotation", annotations
        )["artifact"]
        prediction_artifact = client.import_lens_sensor_isp_perception_artifact(
            "prediction", predictions
        )["artifact"]
        spec = client.lens_sensor_isp_perception_template()["specification"]
        spec.update(
            name=f"dev19 {kind} supplied prediction diagnostic",
            source_lens_sensor_job_id=case["raw_job_id"],
            source_lens_sensor_isp_job_id=case["isp_job_id"],
            annotation_artifact_sha256=annotation_artifact["sha256"],
            prediction_artifact_sha256=prediction_artifact["sha256"],
            model_manifest_sha256=manifest["sha256"],
            model_artifact_sha256="",
            acknowledge_external_predictions=True,
        )
        job, elapsed = _finished(
            client, client.submit_lens_sensor_isp_perception(spec),
            "lens_sensor_isp_perception",
        )
        detail = client.lens_sensor_isp_perception(job["id"])
        evaluation = detail["result"]["summary"]["evaluation"]
        if (
            evaluation["image_binding"] != binding
            or evaluation["verification"]["detector_inference"] != "not_performed"
            or evaluation["metrics"]["dataset_map"]["available"] is not False
            or evaluation["prediction_provenance"]["model_execution_verified"] is not False
            or evaluation["annotation_provenance"]["ground_truth_source"]["source_kind"]
            != "synthetic_contract_fixture"
            or evaluation["verification"]["ground_truth_independence"]
            != "semantic_ground_truth_absent_synthetic_contract_fixture"
        ):
            raise RuntimeError(f"Installed {kind} prediction evaluation overclaims evidence.")
        evaluated.append({
            "scene_kind": kind,
            "job_id": job["id"],
            "status": job["status"],
            "elapsed_s": elapsed,
            "annotation_artifact_sha256": annotation_artifact["sha256"],
            "prediction_artifact_sha256": prediction_artifact["sha256"],
            "model_manifest_sha256": manifest["sha256"],
            "annotation_source_kind": evaluation["annotation_provenance"]
            ["ground_truth_source"]["source_kind"],
            "true_positive": evaluation["metrics"]["true_positive"],
            "false_positive": evaluation["metrics"]["false_positive"],
            "false_negative": evaluation["metrics"]["false_negative"],
        })
    return evaluated


def _cohort(client, transport: TestClient, evaluated: list[dict]) -> dict:
    source_ids = [case["job_id"] for case in evaluated]
    template = client.lens_sensor_isp_perception_cohort_template()
    if not set(source_ids).issubset({row["id"] for row in template["source_jobs"]}):
        raise RuntimeError("Typed cohort template does not list completed inputs.")
    spec = template["specification"]
    spec.update(
        name="dev19 distinct synthetic ISP-frame cohort diagnostic",
        evaluation_job_ids=source_ids,
        iou_thresholds=[0.5, 0.75],
        acknowledge_diagnostic_only=True,
    )
    job, elapsed = _finished(
        client, client.submit_lens_sensor_isp_perception_cohort(spec),
        "lens_sensor_isp_perception_cohort",
    )
    detail = client.lens_sensor_isp_perception_cohort(job["id"])
    export = client.export_lens_sensor_isp_perception_cohort(job["id"])
    result_bytes = _artifact(transport, detail["artifacts"][0])
    summary = detail["result"]["summary"]
    assessment = summary["assessment"]
    metric = assessment["diagnostic_map"]["value"]
    if (
        canonical(export) != canonical(detail)
        or result_bytes != canonical(summary).encode("utf-8")
        or {case["scene_family"] for case in assessment["cases"]}
        != set(SCENE_FAMILIES)
        or metric["image_id_matching_enforced"] is not True
        or metric["image_count"] != 2
        or metric["prediction_count"] != 3
        or metric["ground_truth_count_by_label"] != {"contract_target": 2}
        or not 0.0 < metric["map"] < 1.0
        or assessment["diagnostic_map"]["available"] is not True
        or assessment["product_map"]["available"] is not False
        or assessment["verification"]["ground_truth_independence"]
        != "semantic_ground_truth_absent_synthetic_contract_fixture"
        or any(
            case["annotation_source"]["source_kind"] != "synthetic_contract_fixture"
            for case in assessment["cases"]
        )
        or any(case["model"]["model_execution_verified"] is not False
               for case in assessment["cases"])
        or summary["verification"]["product_perception_performance"]
        != "not_established"
    ):
        raise RuntimeError("Installed cohort export or diagnostic-only scope is invalid.")
    return {
        "job_id": job["id"],
        "status": job["status"],
        "elapsed_s": elapsed,
        "source_evaluation_job_ids": source_ids,
        "case_scene_families": [case["scene_family"] for case in assessment["cases"]],
        "diagnostic_map": metric["map"],
        "image_id_matching_enforced": metric["image_id_matching_enforced"],
        "result_artifact_sha256": detail["artifacts"][0]["sha256"],
        "result_export_byte_equal": True,
        "product_map_available": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.state.exists():
        raise RuntimeError("Use a fresh state directory for this installed-wheel proof.")
    package = _verify_wheel(args.installed_root, args.wheel)
    if package["version"] != "0.16.2.dev19":
        raise RuntimeError("The exact dev19 installed wheel is required.")
    app = create_app(args.state, args.workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed child Worker did not start.")
        sources, sensor = _source_jobs(client, transport)
        evaluated = _evaluation_jobs(client, sources)
        cohort = _cohort(client, transport, evaluated)
        if worker.poll() is not None:
            raise RuntimeError("Installed child Worker stopped before the cohort completed.")
        proof = {
            "schema": "camerae2e_installed_joint_dev19_perception_cohort_proof_v1",
            "package": package,
            "worker_child_pid": worker.pid,
            "lens_revision_id": LENS_REVISION_ID,
            "sensor_revision": {
                "report_code": sensor["report_code"], "run_id": sensor["run_id"],
            },
            "sources": [
                {key: value for key, value in source.items() if key != "image_binding"}
                for source in sources
            ],
            "single_frame_evaluations": evaluated,
            "cohort": cohort,
            "evidence_limit": (
                "Authored boxes on tiny uniform/edge simulation patterns check binding, "
                "image-aware matching, Worker persistence and export only; no "
                "semantic object truth or detector inference is verified."
            ),
            "product_perception_performance": "not_established",
            "physical_validation": "not_established",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "cohort_job_id": cohort["job_id"],
        "diagnostic_map": cohort["diagnostic_map"],
        "product_map_available": False,
        "wheel_sha256": package["wheel_sha256"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
