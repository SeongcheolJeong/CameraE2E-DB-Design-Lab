#!/usr/bin/env python3
"""Audit saved, actual Lens × Sensor detector Jobs without rerunning weights."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from camera_platform import __version__
from camera_platform.lens_sensor_isp_detector_qualification import (
    detector_replay_intake_template,
    qualify_saved_detector_replay,
)
from camera_platform.registry import canonical, digest
from camera_platform.service import PlatformService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--detector-job-id", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not (args.state / "registry.sqlite").is_file():
        raise ValueError("Choose an existing saved Camera Platform Registry state.")
    service = PlatformService(args.state, args.workspace)
    intake = detector_replay_intake_template(service, args.detector_job_id)
    report = qualify_saved_detector_replay(service, intake)
    proof = {
        "schema": "camerae2e_saved_detector_replay_audit_v1",
        "source_version": __version__,
        "detector_job_ids": args.detector_job_id,
        "intake": intake,
        "report": report,
        "report_sha256": digest(report),
        "replay_performed_by_this_audit": False,
        "product_map_available": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "report_sha256": proof["report_sha256"],
        "product_map_available": False,
        "blockers": report["product_map"]["blockers"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
