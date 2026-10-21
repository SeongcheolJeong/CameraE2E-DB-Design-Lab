#!/usr/bin/env python3
"""Check dev11 cohort, supplied-box and PWM-HDR Jobs in installed child Workers.

The inputs are copies of the dev9 evidence states. No new optical capture is
performed; this verifies that dev11 can consume those retained artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
from fastapi.testclient import TestClient

import camera_platform
import pyisetcam
from camera_platform.api import create_app
from camera_platform.registry import canonical
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint
from pyisetcam.dataset import _sha256_array


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _installed_package(installed_root: Path, wheel: Path) -> dict:
    root = installed_root.resolve()
    for module in (camera_platform, pyisetcam):
        if not Path(module.__file__).resolve().is_relative_to(root):
            raise RuntimeError(f"{module.__name__} leaked from the development source tree.")
    if not wheel.name.startswith(f"pyisetcam-{camera_platform.__version__}-"):
        raise RuntimeError("Installed wheel version differs from the imported package.")
    count = 0
    with ZipFile(wheel) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            if member.filename.endswith(".dist-info/RECORD"):
                # pip regenerates RECORD with installed paths and sizes.
                continue
            path = root / member.filename
            if not path.is_file() or path.read_bytes() != archive.read(member):
                raise RuntimeError(f"Installed package differs from wheel: {member.filename}")
            count += 1
    return {
        "wheel": str(wheel.resolve()), "wheel_sha256": _sha(wheel.read_bytes()),
        "wheel_files_matching_installed_bytes": count,
        "camera_platform_path": str(Path(camera_platform.__file__).resolve()),
        "pyisetcam_path": str(Path(pyisetcam.__file__).resolve()),
    }


def _copied_state(copy: Path, original: Path) -> dict:
    copy, original = copy.resolve(), original.resolve()
    if copy == original or copy.is_relative_to(original) or original.is_relative_to(copy):
        raise RuntimeError("Installed verification must use a separate copied evidence state.")
    registry, source = copy / "registry.sqlite", original / "registry.sqlite"
    if (
        not registry.is_file()
        or not source.is_file()
        or registry.read_bytes() != source.read_bytes()
    ):
        raise RuntimeError("Evidence-state copy does not match the pinned source registry.")
    return {
        "path": str(copy), "copied_from": str(original),
        "registry_sha256_before_jobs": _sha(registry.read_bytes()),
    }


def _client(transport: TestClient) -> PlatformClient:
    client = object.__new__(PlatformClient)
    client.http = transport
    response = transport.get("/api/v1/session")
    response.raise_for_status()
    transport.headers["X-Platform-Token"] = response.json()["token"]
    return client


def _artifact(transport: TestClient, descriptor: dict) -> bytes:
    response = transport.get(descriptor["url"])
    response.raise_for_status()
    content = response.content
    if _sha(content) != descriptor["sha256"] or len(content) != descriptor["bytes"]:
        raise RuntimeError("Registry artifact differs from its descriptor.")
    return content


def _cohort(workspace: Path, state_dir: Path, matrix: dict) -> dict:
    app = create_app(state_dir, workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed cohort child Worker did not start.")
        selected_cases = matrix["cases"][:2]
        ids = [item["job_id"] for item in selected_cases]
        original = []
        for identity, case in zip(ids, selected_cases, strict=True):
            source = client.lens_sensor_raw(identity)
            if source["status"] != "succeeded":
                raise RuntimeError("A retained matrix RAW source is incomplete.")
            actual_artifacts = {item["name"]: item["sha256"] for item in source["artifacts"]}
            if (
                actual_artifacts.get("lens-sensor-original-raw.npy")
                != case["raw_artifact"]["sha256"]
                or actual_artifacts.get("lens-sensor-raw-preview-result.json")
                != case["result_artifact"]["sha256"]
            ):
                raise RuntimeError("Copied Lens RAW source differs from the dev9 matrix proof.")
            content = _artifact(
                transport, source["result"]["summary"]["preview"]["raw_artifact"]
            )
            array = np.load(io.BytesIO(content), allow_pickle=False)
            if _sha256_array(array) != case["raw_content_sha256"]:
                raise RuntimeError("Copied Lens RAW values differ from the dev9 matrix proof.")
            original.append(array)
        template = client.lens_sensor_raw_cohort_template()
        specification = template["specification"]
        specification.update(
            name="installed retained two-source RAW cohort",
            source_jobs=[
                {"source_job_id": ids[0], "split": "train"},
                {"source_job_id": ids[1], "split": "test"},
            ],
            acknowledge_simulation_limits=True,
        )
        queued = client.submit_lens_sensor_raw_cohort(specification)
        result = client.wait(queued["id"], expected_kind="lens_sensor_raw_cohort", timeout=180)
        if result["status"] != "succeeded":
            raise RuntimeError(f"Installed cohort Job failed: {result.get('error')}")
        summary = result["result"]["summary"]
        if (
            summary["case_count"] != 2
            or summary["dataset_validation"]["ok"] is not True
            or result["environment"] != environment_fingerprint()
        ):
            raise RuntimeError("Cohort lacks two validated RAW records.")
        bundle = _artifact(transport, summary["bundle"])
        exported = transport.get(f"/api/v1/lens-sensor-raw-cohorts/{result['id']}/export")
        exported.raise_for_status()
        if exported.content != bundle:
            raise RuntimeError("Cohort export differs from the retained ZIP artifact.")
        with ZipFile(io.BytesIO(bundle)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            records = manifest["records"]
            if (
                len(records) != 2
                or {item["split"] for item in records} != {"train", "test"}
                or manifest["split_policy"]["holdout_independence_claimed"] is not False
            ):
                raise RuntimeError("Cohort split manifest differs from the selected sources.")
            for identity, expected in zip(ids, original, strict=True):
                record = next(
                    item for item in records
                    if any(binding["source_lens_sensor_job_id"] == identity
                           for binding in item["source_bindings"])
                )
                with np.load(io.BytesIO(archive.read(record["raw"])), allow_pickle=False) as arrays:
                    saved = arrays["raw"]
                    if (saved.dtype != np.dtype("int32") or saved.shape != expected.shape
                            or saved.tobytes(order="C") != expected.tobytes(order="C")):
                        raise RuntimeError("Cohort changed an original int32 RAW frame.")
        return {
            "job_id": result["id"],
            "status": result["status"],
            "worker_child_pid": worker.pid,
            "source_job_ids": ids,
            "source_raw_exact": True,
            "case_count": summary["case_count"],
            "splits": manifest["splits"],
            "bundle_sha256": summary["bundle"]["sha256"],
            "global_holdout_claimed": False,
        }


def _perception_one(client: PlatformClient, raw_id: str, isp_id: str) -> dict:
    template = client.lens_sensor_isp_perception_template(
        source_lens_sensor_job_id=raw_id,
        source_lens_sensor_isp_job_id=isp_id,
    )
    binding = template["image_binding"]
    labels = {"id": "installed-fixture-labels", "version": "1", "labels": ["object"]}
    model_bytes = b"declared external model identity; model inference is not performed"
    model_sha = _sha(model_bytes)
    manifest = {
        "schema": "camerae2e_detection_model_manifest_v1",
        "id": "declared-external-model", "version": "1",
        "backend": "external_predictions", "checkpoint_sha256": model_sha,
        "label_set": labels,
        "preprocessing": {
            "input_color_encoding": binding["color_encoding"],
            "input_shape": binding["shape"], "input_dtype": binding["dtype"],
            "channel_order": "RGB", "resize": "none",
        },
    }
    model_artifact = client.import_lens_sensor_isp_perception_artifact(
        "model_manifest", manifest
    )["artifact"]
    authored_fixture = {
        "schema": "camerae2e_authored_box_fixture_v1",
        "purpose": "API and Worker wiring only; no independently verified truth",
        "image_size_row_column": binding["shape"][:2],
        "objects": [{"bbox": [1, 1, 4, 4], "label": "object"}],
    }
    fixture_bytes = (canonical(authored_fixture) + "\n").encode("utf-8")
    annotations = {
        "schema": "camerae2e_detection_annotations_v1",
        "image_binding": binding,
        "annotation_set": {"id": "authored-fixture-boxes", "version": "1"},
        "ground_truth_provenance": {
            "source_kind": "independent_reference",
            "source_uri": "fixture://authored-boxes",
            "source_dataset_id": "separate-authored-box-fixture",
            "source_dataset_sha256": _sha(fixture_bytes),
            "method_and_version": "authored-fixture:1",
            "predictions_used_for_annotation": False,
            "uncertainty": {
                "kind": "unknown", "reason": "Authored boxes are not independently validated."
            },
        },
        "label_set": labels,
        "objects": [{"bbox": [1, 1, 4, 4], "label": "object"}],
    }
    annotation_artifact = client.import_lens_sensor_isp_perception_artifact(
        "annotation", annotations
    )["artifact"]
    predictions = {
        "schema": "camerae2e_detection_predictions_v1",
        "image_binding": binding,
        "label_set": labels,
        "model_manifest_sha256": model_artifact["sha256"],
        "model_artifact_sha256": model_sha,
        "inference_run_id": "authored-external-prediction-fixture",
        "detections": [{"bbox": [1, 1, 4, 4], "label": "object", "score": 0.9}],
    }
    prediction_artifact = client.import_lens_sensor_isp_perception_artifact(
        "prediction", predictions
    )["artifact"]
    specification = template["specification"]
    specification.update(
        name="installed supplied-box evaluation",
        source_lens_sensor_job_id=raw_id,
        source_lens_sensor_isp_job_id=isp_id,
        annotation_artifact_sha256=annotation_artifact["sha256"],
        prediction_artifact_sha256=prediction_artifact["sha256"],
        model_manifest_sha256=model_artifact["sha256"],
        acknowledge_external_predictions=True,
    )
    queued = client.submit_lens_sensor_isp_perception(specification)
    result = client.wait(queued["id"], expected_kind="lens_sensor_isp_perception", timeout=180)
    if result["status"] != "succeeded":
        raise RuntimeError(f"Installed supplied-box Job failed: {result.get('error')}")
    summary = result["result"]["summary"]
    evaluation = summary["evaluation"]
    if (
        evaluation["metrics"]["true_positive"] != 1
        or evaluation["metrics"]["dataset_map"]["available"] is not False
        or evaluation["verification"]["detector_inference"] != "not_performed"
        or summary["source_identities"]["image_binding"] != binding
        or result["environment"] != environment_fingerprint()
    ):
        raise RuntimeError("Installed prediction result crossed its truth boundary.")
    exported = client.http.get(
        f"/api/v1/lens-sensor-isp-perception/{result['id']}/export"
    )
    exported.raise_for_status()
    return {
        "job_id": result["id"], "status": result["status"],
        "source_isp_job_id": isp_id,
        "candidate_rgb_dtype": binding["dtype"],
        "candidate_rgb_sha256": binding["array_sha256"],
        "metric": "single_frame_supplied_box_matching_only",
        "detector_inference_performed": False,
        "ground_truth_independence_verified": False,
        "authored_box_fixture": {
            "document": authored_fixture, "source_bytes_sha256": _sha(fixture_bytes),
            "byte_encoding": "canonical_json_plus_newline_utf8",
        },
        "result_sha256": _sha(exported.content),
    }


def _pwm_hdr_one(client: PlatformClient, joint: dict) -> dict:
    lfm_id = joint["records"]["lens_sensor_lfm"]["id"]
    source = client.lens_sensor_lfm(lfm_id)
    if (
        source["status"] != "succeeded"
        or sorted(item["sha256"] for item in source["artifacts"])
        != sorted(joint["records"]["lens_sensor_lfm"]["artifact_sha256"])
    ):
        raise RuntimeError("Copied PWM/LFM source differs from its dev9 proof.")
    source_npz = _artifact(
        client.http, source["result"]["summary"]["report"]["artifacts"]["npz"]
    )
    with np.load(io.BytesIO(source_npz), allow_pickle=False) as archive:
        original = archive["original_pwm_raw_dn"].copy()
    template = client.lens_sensor_pwm_hdr_template()
    specification = template["specification"]
    specification.update(
        name="installed PWM HDR replay from retained Lens Sensor RAW",
        source_lfm_job_id=lfm_id,
        acknowledge_simulation_limits=True,
    )
    queued = client.submit_lens_sensor_pwm_hdr(specification)
    result = client.wait(queued["id"], expected_kind="lens_sensor_pwm_hdr", timeout=180)
    if result["status"] != "succeeded":
        raise RuntimeError(f"Installed PWM HDR Job failed: {result.get('error')}")
    summary = result["result"]["summary"]
    if (
        result["environment"] != environment_fingerprint()
        or summary["source_lfm_job_id"] != lfm_id
        or summary["status"] != "inconclusive"
        or summary["fused_hdr_pixels"] + summary["single_bracket_fallback_pixels"]
        + summary["invalid_pixels"] != original.shape[1] * original.shape[2] * original.shape[3]
    ):
        raise RuntimeError("Installed PWM HDR status or source/coverage binding changed.")
    evidence = summary["evidence"]
    npz = _artifact(client.http, evidence["npz"])
    _artifact(client.http, evidence["json"])
    with np.load(io.BytesIO(npz), allow_pickle=False) as archive:
        replayed = archive["original_pwm_raw_dn"]
        if (
            replayed.dtype != np.dtype("int32")
            or replayed.shape != original.shape
            or replayed.tobytes(order="C") != original.tobytes(order="C")
        ):
            raise RuntimeError("PWM HDR replay changed the retained integer RAW bracket.")
    exported = client.http.get(f"/api/v1/lens-sensor-pwm-hdr/{result['id']}/export")
    exported.raise_for_status()
    return {
        "job_id": result["id"],
        "status": result["status"],
        "coverage_status": summary["status"],
        "source_lfm_job_id": lfm_id,
        "original_int32_pwm_raw_exact": True,
        "fused_hdr_pixels": summary["fused_hdr_pixels"],
        "single_bracket_fallback_pixels": summary["single_bracket_fallback_pixels"],
        "invalid_pixels": summary["invalid_pixels"],
        "result_sha256": _sha(exported.content),
        "product_lfm_claimed": False,
    }


def _perception(workspace: Path, state_dir: Path, joint: dict, calibrated: dict) -> dict:
    app = create_app(state_dir, workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed perception child Worker did not start.")
        raw_id = joint["records"]["lens_sensor_raw"]["id"]
        jobs = {
            "replay": joint["records"]["lens_sensor_isp_replay"]["id"],
            "calibrated": calibrated["job_id"],
        }
        expected = {
            raw_id: joint["records"]["lens_sensor_raw"]["artifact_sha256"],
            jobs["replay"]: joint["records"]["lens_sensor_isp_replay"]["artifact_sha256"],
            jobs["calibrated"]: calibrated["artifact_sha256"],
        }
        for identity, hashes in expected.items():
            source = client.get("jobs/" + identity)
            if source["status"] != "succeeded":
                raise RuntimeError("A dev9 Lens source Job is incomplete.")
            detail = client.get(source["detail_path"])
            if sorted(item["sha256"] for item in detail["artifacts"]) != sorted(hashes):
                raise RuntimeError("Copied Lens source artifacts differ from the dev9 proof.")
        results = {kind: _perception_one(client, raw_id, identity)
                   for kind, identity in jobs.items()}
        pwm_hdr = _pwm_hdr_one(client, joint)
        return {
            "worker_child_pid": worker.pid,
            "source_raw_job_id": raw_id,
            "jobs": results,
            "pwm_hdr": pwm_hdr,
        }


def verify(
    workspace: Path, installed_root: Path, wheel: Path, cohort_state: Path,
    perception_state: Path,
    matrix_proof: Path, joint_proof: Path, calibrated_proof: Path,
) -> dict:
    package = _installed_package(installed_root, wheel)
    matrix = json.loads(matrix_proof.read_text(encoding="utf-8"))
    joint = json.loads(joint_proof.read_text(encoding="utf-8"))
    calibrated = json.loads(calibrated_proof.read_text(encoding="utf-8"))
    if (
        matrix.get("package", {}).get("version") != "0.16.2.dev9"
        or joint.get("version") != "0.16.2.dev9"
        or calibrated.get("version") != "0.16.2.dev9"
    ):
        raise RuntimeError("Retained source Jobs must come from the pinned dev9 proofs.")
    if any(record.get("status") != "succeeded" for record in matrix["cases"]):
        raise RuntimeError("The selected Lens x Sensor matrix is incomplete.")
    if calibrated["job_status"] != "succeeded":
        raise RuntimeError("The calibrated ISP source is incomplete.")
    if cohort_state.resolve() == perception_state.resolve():
        raise RuntimeError("Cohort and perception/PWM verification states must be separate.")
    cohort_copy = _copied_state(cohort_state, Path(matrix["state_dir"]))
    perception_copy = _copied_state(perception_state, Path(joint["state_dir"]))
    result = {
        "schema": "camera_platform_joint_dev11_installed_proof_v1",
        "version": camera_platform.__version__,
        "package": package,
        "source_state_copies": {"cohort": cohort_copy, "perception_pwm": perception_copy},
        "source_retained_proofs": [
            {"path": str(path), "sha256": _sha(path.read_bytes())}
            for path in (matrix_proof, joint_proof, calibrated_proof)
        ],
        "cohort": _cohort(workspace, cohort_state, matrix),
        "supplied_box_perception": _perception(
            workspace, perception_state, joint, calibrated
        ),
        "boundaries": {
            "new_optical_capture_performed": False,
            "source_raw": "retained_dev9_simulated_8x8_int32",
            "dataset": "unlabeled_two_source_cohort_no_global_holdout",
            "perception": "supplied_boxes_single_frame_no_detector_or_map",
            "pwm_hdr": "model_internal_static_pwm_with_inconclusive_coverage",
            "physical_validation": "not_established",
            "product_approval": "not_granted",
        },
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--cohort-state", type=Path, required=True)
    parser.add_argument("--perception-state", type=Path, required=True)
    parser.add_argument("--matrix-proof", type=Path, required=True)
    parser.add_argument("--joint-proof", type=Path, required=True)
    parser.add_argument("--calibrated-proof", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(f"Refusing to replace existing installed proof: {args.output}")
    result = verify(
        args.workspace, args.installed_root, args.wheel, args.cohort_state,
        args.perception_state,
        args.matrix_proof, args.joint_proof, args.calibrated_proof,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "version": result["version"],
        "cohort_status": result["cohort"]["status"],
        "perception_jobs": {
            key: value["status"]
            for key, value in result["supplied_box_perception"]["jobs"].items()
        },
        "pwm_hdr_coverage": result["supplied_box_perception"]["pwm_hdr"]["coverage_status"],
    }))


if __name__ == "__main__":
    main()
