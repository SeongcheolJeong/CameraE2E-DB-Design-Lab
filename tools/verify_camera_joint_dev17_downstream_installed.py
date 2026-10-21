#!/usr/bin/env python3
"""Installed-wheel proof for reusable ISP calibration and annotated RAW cohort."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tempfile
from pathlib import Path
from zipfile import ZipFile

import numpy as np
from fastapi.testclient import TestClient
from verify_camera_bounded_psf_installed import _artifact, _client, _verify_wheel

from camera_platform.api import create_app
from camera_platform.registry import canonical


def _finish(client, submitted: dict, kind: str) -> dict:
    result = client.wait(submitted["id"], expected_kind=kind, timeout=240)
    if result["status"] != "succeeded":
        raise RuntimeError(f"Installed {kind} failed: {result.get('error')}")
    return result


def _calibration(binding: dict) -> dict:
    rows, columns = binding["geometry"]["full_sensor_shape"]
    return {
        "schema": "camerae2e_lens_sensor_isp_calibration_v2",
        "provenance": {
            "source_kind": "simulated",
            "source_uri": "fixture://declared-independent-lens-flat-dark-series",
            "method_and_version": "authored_numeric_fixture:1",
            "source_dataset_id": "declared-independent-lens-flat-dark-series",
            "source_dataset_sha256": "a" * 64,
            "target_raw_used_for_fitting": False,
            "uncertainty": {
                "kind": "unknown", "reason": "Independence is declared, not verified."
            },
        },
        "binding": binding,
        "payload": {
            "schema": "camera_isp_calibration_v1",
            "sites": ["R", "Gr", "Gb", "B"],
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
        },
    }


def _capture(client, scene: str, seed: int) -> dict:
    template = client.lens_sensor_raw_template(scope="central_bounded_dense_common_scene")
    sensor = next(
        row for row in template["sensor_catalog"]
        if row["report_code"] == "DEF-2512-801" and row["model"] == "db_estimate_v1"
    )
    spec = template["specification"]
    spec.update(
        name=f"dev17 ISP {scene} seed {seed}",
        lens_revision_id="6c48e967-74b4-5901-8186-ba6fafe4797b",
        sensor_revision={"identifier": sensor["report_code"], "run_id": sensor["run_id"]},
        scene_kind=scene, sensor_size=[16, 16], scene_samples_row_col=[128, 128],
        scene_horizontal_fov_deg=0.12, luminance_cd_m2=1000.0,
        exposure_s=0.004, noise_flag=2, seed=seed,
        accept_approximate_lens_radiometry=True,
    )
    return _finish(client, client.submit_lens_sensor_raw(spec), "lens_sensor_raw")


def _isp(client, transport: TestClient) -> dict:
    first = _capture(client, "uniform", 7)
    second = _capture(client, "slanted_edge", 17)
    templates = [
        client.lens_sensor_calibrated_isp_template(
            source_lens_sensor_job_id=source["id"]
        ) for source in (first, second)
    ]
    binding = templates[0]["expected_reusable_calibration_binding"]
    if (
        binding != templates[1]["expected_reusable_calibration_binding"]
        or templates[0]["expected_calibration_binding"]
        == templates[1]["expected_calibration_binding"]
    ):
        raise RuntimeError("Reusable ISP calibration applicability is inconsistent.")
    with tempfile.TemporaryDirectory(prefix="camera-dev17-isp-") as temporary:
        path = Path(temporary) / "declared-calibration.json"
        path.write_text(
            json.dumps(_calibration(binding), sort_keys=True, separators=(",", ":")) + "\n"
        )
        imported = client.import_lens_sensor_isp_calibration(path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if imported["calibration_sha256"] != sha:
            raise RuntimeError("Imported calibration bytes changed.")
        results = []
        for source, template in zip((first, second), templates, strict=True):
            source_report = source["result"]["summary"]["preview"]
            original = np.load(io.BytesIO(_artifact(transport, source_report["raw_artifact"])),
                               allow_pickle=False)
            spec = template["specification"]
            spec.update(
                source_lens_sensor_job_id=source["id"], calibration_sha256=sha,
                acknowledge_simulation_limits=True,
            )
            spec["profile"]["calibration_id"] = "sha256:" + sha
            spec["profile"]["spatial_lsc"]["mode"] = "native_four_site_grid_v1"
            spec["profile"]["raw_denoise"].update(
                mode="calibrated_same_site_filter_v1", strength=0.8
            )
            finished = _finish(
                client, client.submit_lens_sensor_calibrated_isp(spec),
                "lens_sensor_calibrated_isp",
            )
            saved = client.lens_sensor_calibrated_isp(finished["id"])["result"]["summary"]
            with np.load(io.BytesIO(_artifact(transport, saved["evidence"]["npz"])),
                         allow_pickle=False) as evidence:
                retained = np.asarray(evidence["source_raw_dn_frame"])
            if (
                saved["calibration_binding"] != binding
                or saved["calibration"]["schema"]
                != "camerae2e_lens_sensor_isp_calibration_v2"
                or saved["raw_values_byte_equal"] is not True
                or retained.dtype != np.dtype("int32")
                or retained.tobytes() != original.tobytes()
                or saved["before_after"]["default_vs_candidate"][
                    "quality_improvement_claimed"
                ] is not False
            ):
                raise RuntimeError("Installed reusable ISP changed source RAW or overclaimed.")
            results.append({
                "source_job_id": source["id"], "isp_job_id": finished["id"],
                "scene_kind": source["specification"]["scene_kind"],
                "seed": source["specification"]["seed"],
                "source_raw_artifact_sha256": source_report["raw_artifact"]["sha256"],
                "retained_raw_byte_equal": True,
            })
    return {
        "calibration_artifact_sha256": sha,
        "calibration_schema": "camerae2e_lens_sensor_isp_calibration_v2",
        "applicability_binding": binding,
        "cases": results,
        "calibration_source_independence": "supplier_declared_not_independently_verified",
    }


def _cohort(client, transport: TestClient, source_ids: list[str]) -> dict:
    annotation_ids = []
    originals = {}
    for source_id, label in zip(source_ids, ("vehicle", "pedestrian"), strict=True):
        source = client.lens_sensor_raw(source_id)
        raw_artifact = source["result"]["summary"]["preview"]["raw_artifact"]
        originals[source_id] = np.load(
            io.BytesIO(_artifact(transport, raw_artifact)), allow_pickle=False
        )
        template = client.retained_raw_dataset_template(
            source_kind="lens_sensor_raw_labeled", source_job_id=source_id
        )
        document = template["annotation_template"]
        document["provenance"].update(
            provider_id="declared-reviewer", annotation_set_id=f"dev17-{source_id}"
        )
        document["objects"] = [{"label": label, "bbox_xyxy": [1, 1, 6, 6]}]
        imported = client.import_retained_raw_annotation(document)
        annotation_ids.append(imported["artifact"]["sha256"])
    template = client.lens_sensor_raw_cohort_template()
    spec = template["annotated_specification"]
    spec.update(
        source_jobs=[
            {"source_job_id": source_id, "split": "train",
             "annotation_artifact_sha256": annotation_id}
            for source_id, annotation_id in zip(source_ids, annotation_ids, strict=True)
        ],
        acknowledge_simulation_limits=True,
    )
    finished = _finish(
        client, client.submit_lens_sensor_raw_cohort(spec), "lens_sensor_raw_cohort"
    )
    summary = client.lens_sensor_raw_cohort(finished["id"])["result"]["summary"]
    exported = transport.get(f"/api/v1/lens-sensor-raw-cohorts/{finished['id']}/export")
    exported.raise_for_status()
    if (
        hashlib.sha256(exported.content).hexdigest() != summary["bundle"]["sha256"]
        or summary["schema"] != "camerae2e_lens_sensor_raw_cohort_study_v2"
        or summary["labels"] != "user_provided_unverified"
        or summary["perception_metric"]["available"] is not False
        or summary["annotation_provenance"]["independence_verified"] is not False
        or summary["dataset_validation"]["ok"] is not True
    ):
        raise RuntimeError("Installed annotated RAW cohort contract changed.")
    with ZipFile(io.BytesIO(exported.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest["splits"] != {"train": 2}:
            raise RuntimeError("Installed annotated cohort split contract changed.")
        for record in manifest["records"]:
            source_id = record["source_binding"]["source_lens_sensor_job_id"]
            with np.load(io.BytesIO(archive.read(record["raw"])), allow_pickle=False) as data:
                raw = np.asarray(data["raw"])
            annotation_sha = hashlib.sha256(
                archive.read(record["source_annotation"])
            ).hexdigest()
            labels = json.loads(archive.read(record["labels"]))["labels"]
            if (
                source_id not in originals
                or raw.tobytes() != originals[source_id].tobytes()
                or annotation_sha not in annotation_ids
                or labels["source_annotation_sha256"] != annotation_sha
                or labels["source"] != "user_provided_unverified"
            ):
                raise RuntimeError("Installed cohort changed exact RAW or source annotation.")
    return {
        "job_id": finished["id"], "source_job_ids": source_ids,
        "bundle_sha256": summary["bundle"]["sha256"],
        "class_map": summary["class_map"],
        "case_count": summary["case_count"],
        "raw_and_annotation_exact": True,
        "ground_truth_and_holdout_independence": "not_established",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--joint-proof", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package = _verify_wheel(args.installed_root, args.wheel)
    joint = json.loads(args.joint_proof.read_text())
    if (
        package["version"] != "0.16.2.dev17"
        or joint["package"]["wheel_sha256"] != package["wheel_sha256"]
        or joint["schema"] != "camerae2e_installed_joint_dev17_proof_v1"
    ):
        raise RuntimeError("Dense RAW and downstream proofs require the same dev17 wheel.")
    app = create_app(args.state, args.workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed child Worker did not start.")
        isp = _isp(client, transport)
        cases = joint["dense_common_scene_joint_quality"]["cases"]
        cohort = _cohort(client, transport, [cases["L1S1"]["job_id"], cases["L2S2"]["job_id"]])
        if worker.poll() is not None:
            raise RuntimeError("Installed child Worker stopped during downstream jobs.")
        proof = {
            "schema": "camerae2e_installed_joint_dev17_downstream_proof_v1",
            "package": package,
            "worker_child_pid": worker.pid,
            "reusable_calibrated_isp": isp,
            "annotated_raw_cohort": cohort,
            "physical_validation": "not_established",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output), "wheel_sha256": package["wheel_sha256"],
        "isp_job_ids": [item["isp_job_id"] for item in isp["cases"]],
        "cohort_job_id": cohort["job_id"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
