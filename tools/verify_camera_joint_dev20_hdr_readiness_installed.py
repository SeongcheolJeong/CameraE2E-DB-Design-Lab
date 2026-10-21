#!/usr/bin/env python3
"""Installed Worker proof that HDR coverage and product readiness stay separate."""

from __future__ import annotations

import argparse
import copy
import io
import json
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from verify_camera_bounded_psf_installed import _artifact, _client, _verify_wheel
from verify_camera_joint_dev18_timed_installed import (
    LENS_REVISION_ID,
    SENSOR_REPORT_CODE,
    _hdr,
    _import_mode,
    _timed_lfm,
)

from camera_platform.api import create_app
from camera_platform.hdr_lfm_evidence_readiness import assess_hdr_lfm_evidence
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
    if package["version"] != "0.16.2.dev20":
        raise RuntimeError("Dev20 installed wheel is required.")
    if args.state.exists():
        raise RuntimeError("A fresh isolated Registry state is required.")
    app = create_app(args.state, args.workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed child Worker did not start.")
        template = client.lens_sensor_lfm_template(scope="central_bounded_timed")
        sensor = next(
            row for row in template["sensor_catalog"]
            if row["report_code"] == SENSOR_REPORT_CODE
            and row["model"] == "db_estimate_v1"
        )
        spec = template["specification"]
        spec.update(
            name="dev20 HDR evidence readiness",
            lens_revision_id=LENS_REVISION_ID,
            sensor_revision={
                "identifier": SENSOR_REPORT_CODE, "run_id": sensor["run_id"],
            },
            sensor_size=[8, 8],
            accept_approximate_lens_radiometry=True,
            acknowledge_simulation_limits=True,
        )
        response = transport.post(
            "/api/v1/lens-sensor-lfm/timing-modes/template",
            json={
                "sensor_revision": spec["sensor_revision"],
                "sensor_size": spec["sensor_size"],
                "exposure_s": spec["exposure_s"],
            },
        )
        response.raise_for_status()
        suggested = response.json()
        if suggested["source_sensor_timing_available"] is not False:
            raise RuntimeError("Stored Sensor unexpectedly claims a product timing mode.")
        mode = copy.deepcopy(suggested["timing_mode_document_template"])
        mode["mode_id"] = "dev20-authored-40us-sensitivity"
        mode["timing"]["line_time_s"] = 40e-6
        mode_sha, _ = _import_mode(transport, mode)
        lfm, original = _timed_lfm(client, transport, spec, mode, mode_sha)
        hdr = _hdr(
            client, transport, lfm["job_id"], original, mode_sha,
            mode["timing"]["line_time_s"],
        )
        lfm_summary = client.lens_sensor_lfm(lfm["job_id"])["result"]["summary"]
        hdr_summary = client.lens_sensor_pwm_hdr(hdr["job_id"])["result"]["summary"]
        report = hdr_summary["report"]
        with np.load(io.BytesIO(_artifact(transport, hdr_summary["evidence"]["npz"])),
                     allow_pickle=False) as archive:
            retained = np.asarray(archive["original_pwm_raw_dn"]).copy()
        readiness = assess_hdr_lfm_evidence(
            report, original_raw_dn=retained, source_lfm_summary=lfm_summary,
        )
        coverage = readiness["model_internal_coverage"]
        if (
            coverage["status"] != "inconclusive"
            or coverage["two_bracket_fused_pixels"] != hdr["fused_hdr_pixels"]
            or coverage["single_bracket_fallback_pixels"]
            != hdr["single_bracket_fallback_pixels"]
            or coverage["invalid_pixels"] != hdr["invalid_pixels"]
            or readiness["source"]["original_raw_bytes_reverified"] is not True
            or readiness["source"]["source_lfm_result_reverified"] is not True
            or readiness["source"]["timing_source_kind"] != "authored_assumption"
            or readiness["source"]["sensor_readout_profile"] != "generic_assumption"
            or readiness["source"]["sensor_readout_classification"]
            != "unmeasured_assumption"
            or readiness["product_hdr_lfm_decision"]["status"] != "not_established"
        ):
            raise RuntimeError("Installed HDR readiness lost evidence or overstated validity.")
        if worker.poll() is not None:
            raise RuntimeError("Installed child Worker stopped during HDR evaluation.")
        proof = {
            "schema": "camerae2e_installed_joint_dev20_hdr_readiness_proof_v1",
            "package": package,
            "worker_child_pid": worker.pid,
            "sensor_revision": spec["sensor_revision"],
            "lens_revision_id": LENS_REVISION_ID,
            "source_sensor_timing_available": False,
            "lfm_job": lfm,
            "hdr_job": hdr,
            "readiness": readiness,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output), "version": package["version"],
        "wheel_sha256": package["wheel_sha256"],
        "hdr_job_id": hdr["job_id"], "coverage": coverage,
        "product_status": readiness["product_hdr_lfm_decision"]["status"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
