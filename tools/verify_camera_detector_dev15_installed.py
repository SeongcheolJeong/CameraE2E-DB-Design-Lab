#!/usr/bin/env python3
"""Verify the opt-in Lens RAW -> ISP RGB -> detector Job from an installed wheel."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import tempfile
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from verify_camera_bounded_psf_installed import _artifact, _client, _verify_wheel

from camera_platform.api import create_app
from camera_platform.registry import canonical


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _finished(client, queued: dict, kind: str) -> dict:
    lifecycle = client.job_status(queued["id"], expected_kind=kind)
    typed_prefix = {
        "lens_sensor_isp_replay": "lens-sensor-isp-replays/",
        "lens_sensor_isp_detector": "lens-sensor-isp-detectors/",
    }[kind]
    if (
        lifecycle["job_kind"] != kind
        or lifecycle["detail_path"] != typed_prefix + queued["id"]
    ):
        raise RuntimeError(f"Common {kind} Job lifecycle lost its typed detail path.")
    finished = client.wait(queued["id"], expected_kind=kind, timeout=180)
    if finished["status"] != "succeeded" or finished["job_kind"] != kind:
        raise RuntimeError(f"Installed {kind} failed: {finished.get('error')}")
    return finished


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--joint-proof", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.state.is_dir():
        raise ValueError("The completed dev15 joint installed state is required.")
    trusted = os.environ.get("CAMERA_PLATFORM_TRUSTED_YOLO_WEIGHTS_SHA256", "")
    if len(trusted) != 64 or any(char not in "0123456789abcdef" for char in trusted):
        raise ValueError("Explicit trusted cached model SHA-256 is required for this dev proof.")
    package = _verify_wheel(args.installed_root, args.wheel)
    if package["version"] != "0.16.2.dev15":
        raise ValueError("This proof requires the dev15 installed package.")
    prior = json.loads(args.joint_proof.read_bytes())
    if (
        prior.get("schema") != "camerae2e_installed_joint_dev15_proof_v1"
        or prior["package"]["wheel_sha256"] != package["wheel_sha256"]
        or prior["package"]["installed_files_matching_wheel"]
        != package["installed_files_matching_wheel"]
    ):
        raise ValueError("Joint installed proof is not from this exact wheel.")
    source = next(item for item in prior["matrix"]["cases"] if item["slot"] == "L1S1")
    app = create_app(args.state, args.workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed child Worker did not restart on saved state.")
        original = client.lens_sensor_raw(source["job_id"])
        raw_descriptor = original["result"]["summary"]["preview"]["raw_artifact"]
        original_bytes = _artifact(transport, raw_descriptor)
        if raw_descriptor["sha256"] != source["raw_artifact_sha256"]:
            raise RuntimeError("Saved source RAW differs from the joint proof.")

        isp_template = client.lens_sensor_isp_replay_template()
        isp_spec = isp_template["specification"]
        if isp_spec["schema"] != "camerae2e_lens_sensor_isp_replay_study_v1":
            raise RuntimeError("Installed Lens ISP replay template is missing.")
        isp_spec.update(
            name="dev15 installed source-bound sRGB for local detector",
            source_lens_sensor_job_id=source["job_id"],
            acknowledge_simulation_limits=True,
        )
        if isp_spec["profile"]["tone"]["curve"] != "srgb":
            raise RuntimeError("The detector requires an explicit sRGB ISP output.")
        isp = _finished(
            client, client.submit_lens_sensor_isp_replay(isp_spec), "lens_sensor_isp_replay"
        )
        isp = client.lens_sensor_isp_replay(isp["id"])
        isp_summary = isp["result"]["summary"]
        if (
            isp_summary["source_binding"]["source_lens_sensor_job_id"] != source["job_id"]
            or isp_summary["raw_values_byte_equal"] is not True
            or isp_summary["candidate_rgb_consumer"]["tone_curve"] != "srgb"
        ):
            raise RuntimeError("Saved sRGB ISP is not bound to the original RAW.")
        with np.load(io.BytesIO(_artifact(transport, isp_summary["evidence"]["npz"])),
                     allow_pickle=False) as arrays:
            rgb = np.asarray(arrays["candidate_rgb"]).copy()
        if rgb.dtype != np.dtype("float32") or rgb.shape != (32, 32, 3):
            raise RuntimeError("Installed ISP candidate RGB has the wrong native shape.")

        # A cached .pt is executable pickle. Template stays locked until an operator
        # declares the exact hash, and that declaration is not origin authentication.
        del os.environ["CAMERA_PLATFORM_TRUSTED_YOLO_WEIGHTS_SHA256"]
        try:
            locked = client.lens_sensor_isp_detector_template()
        finally:
            os.environ["CAMERA_PLATFORM_TRUSTED_YOLO_WEIGHTS_SHA256"] = trusted
        if (
            locked["availability"]["status"] != "operator_trust_required"
            or locked["specification"]["model_weights_sha256"] != ""
        ):
            raise RuntimeError("Detector template did not fail closed without operator trust.")
        template = client.lens_sensor_isp_detector_template(
            source_lens_sensor_job_id=source["job_id"],
            source_lens_sensor_isp_job_id=isp["id"],
        )
        binding = template["image_binding"]
        detector_spec = template["specification"]
        if (
            template["availability"]["status"] != "ready_for_development_inference"
            or detector_spec["model_weights_sha256"] != trusted
            or binding["lens_raw_job_id"] != source["job_id"]
            or binding["lens_isp_job_id"] != isp["id"]
            or binding["color_encoding"] != "srgb"
            or binding["shape"] != [32, 32, 3]
            or binding["array_sha256"] != _sha(rgb.tobytes(order="C"))
        ):
            raise RuntimeError("Detector template lacks exact RAW, RGB or trust binding.")
        detector_spec.update(
            source_lens_sensor_job_id=source["job_id"],
            source_lens_sensor_isp_job_id=isp["id"],
            acknowledge_simulation_limits=True,
        )
        detected = _finished(
            client, client.submit_lens_sensor_isp_detector(detector_spec),
            "lens_sensor_isp_detector",
        )
        detected = client.lens_sensor_isp_detector(detected["id"])
        summary = detected["result"]["summary"]
        inference = summary["inference"]
        rgb_u8 = np.asarray(np.round(rgb * 255.0), dtype=np.uint8)
        model_bgr = np.ascontiguousarray(rgb_u8[..., ::-1])
        if (
            summary["source_identities"]["image_binding"] != binding
            or inference["image_binding"] != binding
            or inference["model_provenance"]["weights_sha256"] != trusted
            or inference["model_provenance"]["operator_trust_is_authenticity_verification"]
            is not False
            or inference["preprocessing"]["rgb_uint8_sha256"]
            != _sha(rgb_u8.tobytes(order="C"))
            or inference["preprocessing"]["model_input_bgr_uint8_sha256"]
            != _sha(model_bgr.tobytes(order="C"))
            or inference["preprocessing"]["numpy_model_input_channel_order"] != "BGR"
            or inference["annotation_evaluation"] is not None
            or inference["detection_count"] != len(inference["detections"])
            or summary["verification"]["product_perception_performance"]
            != "not_established"
        ):
            raise RuntimeError("Installed detector inference lost exact image/model evidence.")
        result_artifact = next(
            item for item in detected["artifacts"]
            if item["name"] == "lens-sensor-isp-detector-result.json"
        )
        result_bytes = _artifact(transport, result_artifact)
        if result_bytes != canonical(summary).encode():
            raise RuntimeError("Installed detector result artifact differs from typed detail.")
        with tempfile.TemporaryDirectory(prefix="camera-dev15-sdk-download-") as directory:
            downloaded = client.download_artifact(
                result_artifact, Path(directory) / "detector.json"
            )
            if downloaded.read_bytes() != result_bytes:
                raise RuntimeError("SDK artifact download changed the saved result.")
        reread = client.lens_sensor_raw(source["job_id"])
        if (
            _artifact(transport, reread["result"]["summary"]["preview"]["raw_artifact"])
            != original_bytes
            or worker.poll() is not None
        ):
            raise RuntimeError("Detector execution changed source RAW or stopped child Worker.")
        proof = {
            "schema": "camerae2e_installed_detector_dev15_proof_v1",
            "package": package,
            "joint_proof_sha256": _sha(args.joint_proof.read_bytes()),
            "source_raw_job_id": source["job_id"],
            "source_raw_artifact_sha256": raw_descriptor["sha256"],
            "isp_job_id": isp["id"],
            "isp_evidence_npz_sha256": isp_summary["evidence"]["npz"]["sha256"],
            "detector_job_id": detected["id"],
            "result_artifact_sha256": result_artifact["sha256"],
            "rgb_uint8_sha256": inference["preprocessing"]["rgb_uint8_sha256"],
            "model_input_bgr_uint8_sha256": inference["preprocessing"][
                "model_input_bgr_uint8_sha256"
            ],
            "model_class_ids": inference["model_class_ids"],
            "detection_count": inference["detection_count"],
            "runtime": inference["runtime"],
            "model_provenance": inference["model_provenance"],
            "operator_trust_required_by_default": True,
            "original_raw_unchanged": True,
            "annotation_evaluation": "not_supplied",
            "physical_validation": "not_established",
            "product_perception_performance": "not_established",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output), "version": package["version"],
        "detector_job_id": proof["detector_job_id"],
        "detection_count": proof["detection_count"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
