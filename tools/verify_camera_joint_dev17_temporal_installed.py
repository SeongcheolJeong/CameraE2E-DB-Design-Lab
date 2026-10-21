#!/usr/bin/env python3
"""Recheck bounded saved PWM/LFM RAW to HDR fusion on the dev17 wheel."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fastapi.testclient import TestClient
from verify_camera_bounded_psf_installed import _client, _verify_wheel
from verify_camera_joint_dev16_installed import _pwm_hdr

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
    if package["version"] != "0.16.2.dev17":
        raise RuntimeError("Dev17 installed wheel is required.")
    app = create_app(args.state, args.workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed child Worker did not start.")
        temporal = _pwm_hdr(client, transport)
        if worker.poll() is not None:
            raise RuntimeError("Installed child Worker stopped during PWM/HDR jobs.")
        proof = {
            "schema": "camerae2e_installed_joint_dev17_temporal_proof_v1",
            "package": package,
            "worker_child_pid": worker.pid,
            "saved_bounded_pwm_hdr": temporal,
            "product_hdr_lfm_performance": "not_established",
            "physical_validation": "not_established",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "lfm_job_id": temporal["lfm_job_id"],
        "hdr_job_id": temporal["hdr_job_id"],
        "hdr_status": temporal["hdr_status"],
        "wheel_sha256": package["wheel_sha256"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
