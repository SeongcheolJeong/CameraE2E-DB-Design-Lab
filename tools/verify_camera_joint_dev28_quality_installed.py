#!/usr/bin/env python3
"""Installed dev28 proof for equal-angle MTF and geometric pixel-cell proxy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fastapi.testclient import TestClient
from verify_camera_bounded_psf_installed import _client, _verify_wheel
from verify_camera_joint_dev18_installed import _joint

from camera_platform.api import create_app
from camera_platform.registry import canonical


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package = _verify_wheel(args.installed_root, args.wheel)
    if package["version"] != "0.16.2.dev28":
        raise RuntimeError("Dev28 installed wheel is required.")
    if args.state.exists():
        raise RuntimeError("A fresh isolated Registry state is required.")
    app = create_app(args.state, args.workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed child Worker did not start.")
        joint = _joint(client, transport)
        shared = joint["quality_comparison"].get("common_grid_object_angle_edge_mtf50") or {}
        footprint = shared.get("model_550nm_point_cell_footprint") or {}
        if (
            shared.get("available") is not True
            or shared.get("angular_roi_area_matched") is not True
            or shared.get("angular_sample_grid_matched") is not True
            or shared.get("product_ranking_allowed") is not False
            or len(shared.get("pairs", [])) != 4
            or not (shared.get("grid") or {}).get("area_square_degrees", 0) > 0
            or footprint.get("available") is not True
            or footprint.get("actual_pixel_integration_or_psf_footprint_compared") is not False
            or footprint.get("max_to_min_median_cell_area_ratio", 0) <= 1.0
            or shared.get("pixel_integrated_effective_area_matched") is not False
        ):
            raise RuntimeError("Installed angular MTF or model cell proxy contract failed.")
        if worker.poll() is not None:
            raise RuntimeError("Installed child Worker stopped during the study.")
        proof = {
            "schema": "camerae2e_installed_joint_dev28_point_cell_quality_proof_v1",
            "package": package,
            "worker_child_pid": worker.pid,
            "dense_common_scene_joint_quality": joint,
            "physical_validation": "not_established",
            "product_ranking_allowed": False,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output), "version": package["version"],
        "wheel_sha256": package["wheel_sha256"],
        "quality_job_id": joint["quality_job_id"],
        "common_grid": shared["grid"],
        "max_to_min_median_cell_area_ratio": footprint["max_to_min_median_cell_area_ratio"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
