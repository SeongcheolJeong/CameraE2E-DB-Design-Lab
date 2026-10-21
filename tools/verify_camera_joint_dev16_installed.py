#!/usr/bin/env python3
"""Exercise joint angular diagnostics and saved PWM HDR from an installed wheel.

The API owns a separate child Worker. This proof records execution and
artifact identity, never physical accuracy or a product comparison.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from verify_camera_bounded_psf_installed import _artifact, _client, _verify_wheel

from camera_platform.api import create_app
from camera_platform.registry import canonical

LENSES = {
    "L1": "6c48e967-74b4-5901-8186-ba6fafe4797b",  # P0014
    "L2": "4daeca8f-5d81-57b0-8ae3-928f23de2b36",  # P0248
}
SENSORS = {"S1": "DEF-2512-801", "S2": "DEF-2412-804"}
SLOTS = ("L1S1", "L1S2", "L2S1", "L2S2")


def _finish(client, submitted: dict, kind: str) -> dict:
    result = client.wait(submitted["id"], expected_kind=kind, timeout=180)
    if result["status"] != "succeeded":
        raise RuntimeError(f"Installed {kind} failed: {result.get('error')}")
    return result


def _joint(client, transport: TestClient) -> dict:
    source_jobs = {}
    cases = {}
    for slot in SLOTS:
        template = client.lens_sensor_raw_template(scope="central_bounded_common_scene")
        if template["specification"]["schema"] != "camerae2e_lens_sensor_raw_preview_study_v4":
            raise RuntimeError("Installed common-scene RAW contract changed.")
        report_code = SENSORS[slot[2:]]
        sensor = next(
            row for row in template["sensor_catalog"]
            if row["report_code"] == report_code and row["model"] == "db_estimate_v1"
        )
        spec = template["specification"]
        spec.update(
            name=f"dev16 point map {slot}",
            lens_revision_id=LENSES[slot[:2]],
            sensor_revision={"identifier": report_code, "run_id": sensor["run_id"]},
            scene_kind="slanted_edge", sensor_size=[8, 8],
            scene_horizontal_fov_deg=0.12, luminance_cd_m2=1000.0,
            exposure_s=0.004, noise_flag=0, seed=7,
            accept_approximate_lens_radiometry=True,
        )
        result = _finish(client, client.submit_lens_sensor_raw(spec), "lens_sensor_raw")
        detail = client.lens_sensor_raw(result["id"])
        summary = detail["result"]["summary"]
        report = summary["preview"]
        native = report["capture"]["native_output_angular_grid"]
        point = native["distortion_aware_point_center"]
        coordinates = np.asarray(point["coordinates_deg_row_col_by_wavelength"])
        raw_artifact = report["raw_artifact"]
        raw = np.load(io.BytesIO(_artifact(transport, raw_artifact)), allow_pickle=False)
        if (
            summary["specification"] != spec
            or coordinates.shape != (3, 8, 8, 2)
            or point["status"] != "model_distortion_aware_pixel_centers_only"
            or point["input_scene_support"]["status"]
            != "all_model_point_centers_within_source_samples"
            or point["cross_sensor_common_output_grid"] != "not_established"
            or raw.dtype != np.dtype("int32") or raw.shape != (8, 8)
            or raw.tolist() != report["raw"]["values"]
        ):
            raise RuntimeError(f"Installed {slot} lost point geometry or exact RAW.")
        source_jobs[slot] = result["id"]
        cases[slot] = {
            "job_id": result["id"],
            "lens_revision_id": spec["lens_revision_id"],
            "sensor_run_id": sensor["run_id"],
            "raw_artifact_sha256": raw_artifact["sha256"],
            "point_center_shape": list(coordinates.shape),
            "minimum_550nm_step_deg_row_col": point[
                "min_adjacent_angular_step_deg_row_col_by_wavelength"
            ][1],
            "paraxial_step_deg_row_col": native["center_angular_step_deg_row_col"],
        }
    matrix = client.lens_sensor_quality_template()["specification"]
    matrix.update(source_jobs=source_jobs, acknowledge_simulation_limits=True)
    quality = _finish(client, client.submit_lens_sensor_quality(matrix),
                      "lens_sensor_quality_study")
    saved = client.lens_sensor_quality(quality["id"])["result"]["summary"]
    comparison = saved["comparison"]
    if (
        saved["specification"] != matrix
        or not all(comparison["distortion_aware_point_center_map_by_slot"].values())
        or comparison["common_550nm_point_center_roi"]["nonempty"] is not True
        or comparison["matched_native_output_angular_grid"] is not False
        or comparison["edge_mtf50_diagnostic_delta_allowed"] is not False
        or comparison["product_ranking_allowed"] is not False
    ):
        raise RuntimeError("Installed 2 x 2 point geometry was promoted to false ranking.")
    return {
        "cases": cases,
        "quality_job_id": quality["id"],
        "point_center_common_550nm_roi": comparison["common_550nm_point_center_roi"],
        "source_step_over_min_distorted_step_by_slot": comparison[
            "source_step_over_min_distorted_step_by_slot"
        ],
        "edge_mtf50_diagnostic_delta_allowed": False,
        "product_ranking_allowed": False,
    }


def _pwm_hdr(client, transport: TestClient) -> dict:
    template = client.lens_sensor_lfm_template(scope="central_bounded")
    if template["specification"]["schema"] != "camerae2e_lens_sensor_pwm_lfm_study_v2":
        raise RuntimeError("Installed bounded PWM/LFM contract changed.")
    sensor = next(
        row for row in template["sensor_catalog"]
        if row["report_code"] == SENSORS["S1"] and row["model"] == "db_estimate_v1"
    )
    spec = template["specification"]
    spec.update(
        name="dev16 bounded PWM source", lens_revision_id=LENSES["L2"],
        sensor_revision={"identifier": sensor["report_code"], "run_id": sensor["run_id"]},
        sensor_size=[8, 8], accept_approximate_lens_radiometry=True,
        acknowledge_simulation_limits=True,
    )
    source = _finish(client, client.submit_lens_sensor_lfm(spec), "lens_sensor_lfm")
    source_report = client.lens_sensor_lfm(source["id"])["result"]["summary"]["report"]
    hdr_spec = client.lens_sensor_pwm_hdr_template()["specification"]
    hdr_spec.update(source_lfm_job_id=source["id"], acknowledge_simulation_limits=True)
    fused = _finish(client, client.submit_lens_sensor_pwm_hdr(hdr_spec), "lens_sensor_pwm_hdr")
    hdr = client.lens_sensor_pwm_hdr(fused["id"])["result"]["summary"]
    source_npz = source_report["artifacts"]["npz"]
    with np.load(io.BytesIO(_artifact(transport, source_npz)), allow_pickle=False) as archive:
        original = np.asarray(archive["original_pwm_raw_dn"])
    with np.load(io.BytesIO(_artifact(transport, hdr["evidence"]["npz"])),
                 allow_pickle=False) as archive:
        fused_original = np.asarray(archive["original_pwm_raw_dn"])
    raw_sha = hashlib.sha256(original.tobytes(order="C")).hexdigest()
    if (
        original.shape != (2, 4, 8, 8) or original.dtype != np.dtype("int32")
        or not np.array_equal(original, fused_original)
        or hdr["report"]["original_pwm_raw_sha256"] != raw_sha
        or hdr["report"]["source_lfm_schema"] != "camerae2e_lens_sensor_pwm_lfm_v2"
        or hdr["report"]["source_psf_compute_mode"] != "bounded_streaming"
        or hdr["status"] != "inconclusive"
        or hdr["fused_hdr_pixels"] + hdr["single_bracket_fallback_pixels"]
        + hdr["invalid_pixels"] != 4 * 8 * 8
    ):
        raise RuntimeError("Installed bounded PWM RAW/HDR fusion contract changed.")
    return {
        "lfm_job_id": source["id"], "hdr_job_id": fused["id"],
        "source_original_raw_sha256": raw_sha,
        "hdr_original_raw_sha256": hdr["report"]["original_pwm_raw_sha256"],
        "hdr_status": hdr["status"],
        "fused_hdr_pixels": hdr["fused_hdr_pixels"],
        "single_bracket_fallback_pixels": hdr["single_bracket_fallback_pixels"],
        "invalid_pixels": hdr["invalid_pixels"],
        "product_hdr_lfm_performance": "not_established",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package = _verify_wheel(args.installed_root, args.wheel)
    if package["version"] != "0.16.2.dev16":
        raise RuntimeError("Dev16 installed wheel is required.")
    if args.state.exists():
        raise RuntimeError("A fresh isolated Registry state is required.")
    app = create_app(args.state, args.workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed child Worker did not start.")
        for path in ("/integration-workbench.html", "/integration-workbench.js"):
            if transport.get(path).status_code != 200:
                raise RuntimeError(f"Installed workbench asset unavailable: {path}")
        joint = _joint(client, transport)
        temporal = _pwm_hdr(client, transport)
        if worker.poll() is not None:
            raise RuntimeError("Installed child Worker stopped during studies.")
        proof = {
            "schema": "camerae2e_installed_joint_dev16_proof_v1",
            "package": package,
            "worker_child_pid": worker.pid,
            "joint_point_geometry": joint,
            "saved_bounded_pwm_hdr": temporal,
            "physical_validation": "not_established",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output), "version": package["version"],
        "wheel_sha256": package["wheel_sha256"],
        "joint_quality_job_id": joint["quality_job_id"],
        "pwm_hdr_job_id": temporal["hdr_job_id"],
        "pwm_hdr_status": temporal["hdr_status"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
