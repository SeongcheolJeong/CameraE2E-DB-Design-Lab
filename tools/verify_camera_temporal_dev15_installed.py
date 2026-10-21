#!/usr/bin/env python3
"""Verify bounded Lens x Sensor static HDR and PWM/LFM from the dev15 wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
from verify_camera_bounded_psf_installed import _client, _verify_wheel
from verify_camera_joint_dev14_installed import _bounded_temporal

from camera_platform.api import create_app
from camera_platform.registry import canonical


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
    if package["version"] != "0.16.2.dev15":
        raise ValueError("Dev15 installed wheel is required.")
    source_bytes = args.joint_proof.read_bytes()
    source = json.loads(source_bytes)
    if (
        source.get("schema") != "camerae2e_installed_joint_dev15_proof_v1"
        or source["package"]["wheel_sha256"] != package["wheel_sha256"]
    ):
        raise ValueError("Joint proof must use the same installed wheel.")
    cases = {case["slot"]: case for case in source["matrix"]["cases"]}
    app = create_app(args.state, args.workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed child Worker did not restart.")
        hdr = _bounded_temporal(client, transport, kind="hdr", case=cases["L1S1"])
        lfm = _bounded_temporal(client, transport, kind="lfm", case=cases["L2S1"])
        if worker.poll() is not None:
            raise RuntimeError("Installed child Worker stopped during temporal jobs.")
        proof = {
            "schema": "camerae2e_installed_temporal_dev15_proof_v1",
            "package": package,
            "joint_proof_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "worker_child_pid": worker.pid,
            "bounded_hdr": hdr,
            "bounded_lfm": lfm,
            "physical_validation": "not_established",
            "product_hdr_lfm_performance": "not_established",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output), "version": package["version"],
        "hdr": hdr["status"], "lfm": lfm["status"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
