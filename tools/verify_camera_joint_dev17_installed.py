#!/usr/bin/env python3
"""Installed-wheel proof for dense common-scene Lens x Sensor RAW and quality gating."""

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
    "L1": "6c48e967-74b4-5901-8186-ba6fafe4797b",
    "L2": "4daeca8f-5d81-57b0-8ae3-928f23de2b36",
}
SENSORS = {"S1": "DEF-2512-801", "S2": "DEF-2412-804"}
SLOTS = ("L1S1", "L1S2", "L2S1", "L2S2")
SOURCE_SIZE = [128, 128]
RAW_SIZE = [16, 16]


def _finish(client, submitted: dict, kind: str) -> dict:
    result = client.wait(submitted["id"], expected_kind=kind, timeout=240)
    if result["status"] != "succeeded":
        raise RuntimeError(f"Installed {kind} failed: {result.get('error')}")
    return result


def _joint(client, transport: TestClient) -> dict:
    source_jobs = {}
    cases = {}
    scene_grids = []
    for slot in SLOTS:
        template = client.lens_sensor_raw_template(
            scope="central_bounded_dense_common_scene"
        )
        if template["specification"]["schema"] != "camerae2e_lens_sensor_raw_preview_study_v5":
            raise RuntimeError("Installed dense common-scene RAW template is missing.")
        sensor_code = SENSORS[slot[2:]]
        sensor = next(
            row for row in template["sensor_catalog"]
            if row["report_code"] == sensor_code and row["model"] == "db_estimate_v1"
        )
        spec = template["specification"]
        spec.update(
            name=f"dev17 dense scene {slot}",
            lens_revision_id=LENSES[slot[:2]],
            sensor_revision={"identifier": sensor_code, "run_id": sensor["run_id"]},
            scene_kind="slanted_edge", sensor_size=RAW_SIZE.copy(),
            scene_horizontal_fov_deg=0.12,
            scene_samples_row_col=SOURCE_SIZE.copy(),
            luminance_cd_m2=1000.0, exposure_s=0.004,
            noise_flag=0, seed=7, accept_approximate_lens_radiometry=True,
        )
        finished = _finish(client, client.submit_lens_sensor_raw(spec), "lens_sensor_raw")
        summary = client.lens_sensor_raw(finished["id"])["result"]["summary"]
        preview = summary["preview"]
        grid = preview["scene"]["angular_grid_contract"]
        point = preview["capture"]["native_output_angular_grid"][
            "distortion_aware_point_center"
        ]
        raw_artifact = preview["raw_artifact"]
        content = _artifact(transport, raw_artifact)
        raw = np.load(io.BytesIO(content), allow_pickle=False)
        minimum = np.asarray(
            point["min_adjacent_angular_step_deg_row_col_by_wavelength"], dtype=float
        )
        source_step = np.asarray(grid["center_angular_step_deg_row_col"], dtype=float)
        ratio = float(np.max(source_step[None, :] / minimum))
        if (
            summary["specification"] != spec
            or grid["source_shape"] != SOURCE_SIZE
            or raw.dtype != np.dtype("int32") or list(raw.shape) != RAW_SIZE
            or raw.tolist() != preview["raw"]["values"]
            or point["cross_sensor_common_output_grid"] != "not_established"
            or point["pixel_integration_and_psf_effective_grid"] != "not_established"
            or ratio >= 1.0
        ):
            raise RuntimeError(f"Installed {slot} lost dense scene or exact RAW evidence.")
        source_jobs[slot] = finished["id"]
        scene_grids.append(grid)
        cases[slot] = {
            "job_id": finished["id"],
            "lens_revision_id": spec["lens_revision_id"],
            "sensor_run_id": sensor["run_id"],
            "source_to_min_distorted_point_step_ratio": ratio,
            "raw_artifact_sha256": hashlib.sha256(content).hexdigest(),
        }
    if any(grid != scene_grids[0] for grid in scene_grids[1:]):
        raise RuntimeError("Installed 2 x 2 source angular scenes are not identical.")
    matrix = client.lens_sensor_quality_template()["specification"]
    matrix.update(source_jobs=source_jobs, acknowledge_simulation_limits=True)
    finished = _finish(
        client, client.submit_lens_sensor_quality(matrix), "lens_sensor_quality_study"
    )
    comparison = client.lens_sensor_quality(finished["id"])["result"]["summary"]["comparison"]
    if (
        comparison["matched_source_controls"] is not True
        or comparison["matched_horizontal_fov"] is not True
        or comparison["input_scene_coarser_than_native_output_by_slot"]
        != dict.fromkeys(SLOTS, False)
        or comparison["matched_native_output_angular_grid"] is not False
        or comparison["edge_mtf50_diagnostic_delta_allowed"] is not False
        or comparison["product_ranking_allowed"] is not False
    ):
        raise RuntimeError("Installed 2 x 2 quality study overclaimed comparability.")
    return {
        "cases": cases,
        "source_scene_shape": SOURCE_SIZE,
        "source_angular_grid": scene_grids[0],
        "quality_job_id": finished["id"],
        "quality_comparison": comparison,
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
    if package["version"] != "0.16.2.dev17":
        raise RuntimeError("Dev17 installed wheel is required.")
    if args.state.exists():
        raise RuntimeError("A fresh isolated Registry state is required.")
    app = create_app(args.state, args.workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed child Worker did not start.")
        joint = _joint(client, transport)
        if worker.poll() is not None:
            raise RuntimeError("Installed child Worker stopped during the study.")
        proof = {
            "schema": "camerae2e_installed_joint_dev17_proof_v1",
            "package": package,
            "worker_child_pid": worker.pid,
            "dense_common_scene_joint_quality": joint,
            "physical_validation": "not_established",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output), "version": package["version"],
        "wheel_sha256": package["wheel_sha256"],
        "quality_job_id": joint["quality_job_id"],
        "source_to_native_ratios": {
            slot: case["source_to_min_distorted_point_step_ratio"]
            for slot, case in joint["cases"].items()
        },
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
