#!/usr/bin/env python3
"""Prove SHA-pinned virtual-row timing survives installed LFM-to-HDR jobs.

The two line times are authored assumptions for a sensitivity study.  The
proof does not establish a vendor sensor mode, its physical timing, or product
HDR/LFM performance.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from verify_camera_bounded_psf_installed import _artifact, _client, _verify_wheel

from camera_platform.api import create_app
from camera_platform.registry import canonical

LENS_REVISION_ID = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"  # P0248
SENSOR_REPORT_CODE = "DEF-2512-801"
LINE_TIMES_S = (20e-6, 40e-6)


def _finish(client, submitted: dict, kind: str) -> dict:
    result = client.wait(submitted["id"], expected_kind=kind, timeout=180)
    if result["status"] != "succeeded":
        raise RuntimeError(f"Installed {kind} failed: {result.get('error')}")
    return result


def _mode_bytes(mode: dict) -> bytes:
    return json.dumps(
        mode, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _import_mode(transport: TestClient, mode: dict) -> tuple[str, dict]:
    content = _mode_bytes(mode)
    sha = hashlib.sha256(content).hexdigest()
    response = transport.post(
        "/api/v1/lens-sensor-lfm/timing-modes/import",
        data={"sha256": sha},
        files={"timing_mode": ("authored-timing-mode.json", content, "application/json")},
    )
    if response.status_code != 201:
        raise RuntimeError(f"Installed timing import failed: {response.text}")
    imported = response.json()
    if (
        imported["timing_mode_sha256"] != sha
        or imported["source_kind"] != "authored_assumption"
        or imported["source_evidence"] is not None
        or imported["verification"]["timing_value"]
        != "declared_not_independently_verified"
        or _artifact(transport, imported["artifact"]) != content
    ):
        raise RuntimeError("Installed timing mode lost authored provenance or exact bytes.")
    return sha, imported


def _timed_lfm(
    client, transport: TestClient, spec: dict, mode: dict, sha: str
) -> tuple[dict, np.ndarray]:
    submitted = client.submit_lens_sensor_lfm({**spec, "timing_mode_sha256": sha})
    finished = _finish(client, submitted, "lens_sensor_lfm")
    detail = client.lens_sensor_lfm(finished["id"])
    summary = detail["result"]["summary"]
    if summary != finished["result"]["summary"]:
        raise RuntimeError("Installed saved LFM result changed on readback.")
    report = summary["report"]
    expected_line = mode["timing"]["line_time_s"]
    if (
        summary["schema"] != "camerae2e_lens_sensor_pwm_lfm_study_v3"
        or report["schema"] != "camerae2e_lens_sensor_pwm_lfm_v3"
        or report["psf_compute_mode"] != "bounded_streaming"
        or report["psf_execution"]["mode"] != "strict_bounded_streaming"
        or report["controls"]["timing_mode_sha256"] != sha
        or report["controls"]["timing_mode"] != mode
        or report["controls"]["line_time_s"] != expected_line
        or report["source_boundary"]["timing_mode_source_kind"]
        != "authored_assumption"
        or report["source_boundary"]["timing_value_verification"]
        != "declared_not_independently_verified"
        or report["cases"][0]["phases"][0]["row_timing"]["row_exposure_start_s"][1]
        != expected_line
    ):
        raise RuntimeError("Installed saved LFM lost strict PSF or timing-mode binding.")
    with np.load(io.BytesIO(_artifact(transport, report["artifacts"]["npz"])),
                 allow_pickle=False) as archive:
        original = np.asarray(archive["original_pwm_raw_dn"]).copy()
    if original.shape != (2, 4, 8, 8) or original.dtype != np.dtype("int32"):
        raise RuntimeError("Installed LFM did not retain the expected integer RAW.")
    return {
        "job_id": finished["id"],
        "line_time_s": expected_line,
        "timing_mode_sha256": sha,
        "lfm_npz_sha256": report["artifacts"]["npz"]["sha256"],
        "original_pwm_raw_sha256": hashlib.sha256(original.tobytes(order="C")).hexdigest(),
        "source_kind": "authored_assumption",
        "timing_value_verification": "declared_not_independently_verified",
        "psf_compute_mode": report["psf_compute_mode"],
    }, original


def _hdr(client, transport: TestClient, lfm_job_id: str,
         original: np.ndarray, mode_sha: str, line_time_s: float) -> dict:
    spec = client.lens_sensor_pwm_hdr_template()["specification"]
    spec.update(
        source_lfm_job_id=lfm_job_id, acknowledge_simulation_limits=True
    )
    finished = _finish(
        client, client.submit_lens_sensor_pwm_hdr(spec), "lens_sensor_pwm_hdr"
    )
    summary = client.lens_sensor_pwm_hdr(finished["id"])["result"]["summary"]
    if summary != finished["result"]["summary"]:
        raise RuntimeError("Installed saved HDR result changed on readback.")
    report = summary["report"]
    with np.load(io.BytesIO(_artifact(transport, summary["evidence"]["npz"])),
                 allow_pickle=False) as archive:
        retained_raw = np.asarray(archive["original_pwm_raw_dn"])
    raw_sha = hashlib.sha256(original.tobytes(order="C")).hexdigest()
    if (
        not np.array_equal(retained_raw, original)
        or report["original_pwm_raw_sha256"] != raw_sha
        or report["source_lfm_job_id"] != lfm_job_id
        or report["source_lfm_schema"] != "camerae2e_lens_sensor_pwm_lfm_v3"
        or report["waveform"]["line_time_s"] != line_time_s
        or report["waveform"]["timing_mode_sha256"] != mode_sha
        or summary["fused_hdr_pixels"] + summary["single_bracket_fallback_pixels"]
        + summary["invalid_pixels"] != 4 * 8 * 8
    ):
        raise RuntimeError("Installed HDR replay lost source timing or exact RAW.")
    return {
        "job_id": finished["id"],
        "source_lfm_job_id": lfm_job_id,
        "status": summary["status"],
        "timing_mode_sha256": mode_sha,
        "line_time_s": line_time_s,
        "source_original_raw_sha256": raw_sha,
        "hdr_original_raw_sha256": report["original_pwm_raw_sha256"],
        "hdr_npz_sha256": summary["evidence"]["npz"]["sha256"],
        "fused_hdr_pixels": summary["fused_hdr_pixels"],
        "single_bracket_fallback_pixels": summary["single_bracket_fallback_pixels"],
        "invalid_pixels": summary["invalid_pixels"],
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
    if package["version"] != "0.16.2.dev18":
        raise RuntimeError("The dev18 installed wheel is required.")
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
            name="dev18 authored row-time sensitivity",
            lens_revision_id=LENS_REVISION_ID,
            sensor_revision={
                "identifier": SENSOR_REPORT_CODE, "run_id": sensor["run_id"]
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
            raise RuntimeError("Saved Sensor unexpectedly claims a source timing mode.")
        imported, results, raws = [], [], []
        for line_time in LINE_TIMES_S:
            mode = copy.deepcopy(suggested["timing_mode_document_template"])
            mode["mode_id"] = f"authored-{round(line_time * 1e6)}us-sensitivity"
            mode["timing"]["line_time_s"] = line_time
            sha, record = _import_mode(transport, mode)
            imported.append({
                "timing_mode_sha256": sha,
                "mode_id": mode["mode_id"],
                "source_kind": record["source_kind"],
                "source_evidence": "not_supplied",
            })
            result, raw = _timed_lfm(client, transport, spec, mode, sha)
            results.append(result)
            raws.append(raw)
        changed_pixels = int(np.count_nonzero(raws[0] != raws[1]))
        if changed_pixels <= 0:
            raise RuntimeError("Different authored row times did not change RAW sensitivity.")
        hdr = _hdr(
            client, transport, results[1]["job_id"], raws[1],
            results[1]["timing_mode_sha256"], results[1]["line_time_s"],
        )
        invalid_mode = copy.deepcopy(suggested["timing_mode_document_template"])
        invalid_mode["mode_id"] = "authored-invalid-binding"
        invalid_mode["binding"]["exposure_s"][1] = 0.008
        invalid_sha, _ = _import_mode(transport, invalid_mode)
        rejected = transport.post(
            "/api/v1/lens-sensor-lfm",
            json={**spec, "timing_mode_sha256": invalid_sha},
        )
        if rejected.status_code != 422 or "Timing mode differs" not in rejected.text:
            raise RuntimeError("Inapplicable timing mode was not rejected at submission.")
        if worker.poll() is not None:
            raise RuntimeError("Installed child Worker stopped during timing jobs.")
        proof = {
            "schema": "camerae2e_installed_joint_dev18_timed_proof_v1",
            "package": package,
            "worker_child_pid": worker.pid,
            "sensor_revision": spec["sensor_revision"],
            "lens_revision_id": LENS_REVISION_ID,
            "source_sensor_timing_available": False,
            "imported_authored_modes": imported,
            "lfm_jobs": results,
            "raw_pixels_changed_between_authored_line_times": changed_pixels,
            "hdr_replay": hdr,
            "mismatched_mode_submission_status": rejected.status_code,
            "physical_timing_verified": False,
            "vendor_hdr_lfm_mode_verified": False,
            "product_hdr_lfm_performance": "not_established",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "version": package["version"],
        "wheel_sha256": package["wheel_sha256"],
        "lfm_job_ids": [item["job_id"] for item in results],
        "raw_pixels_changed": changed_pixels,
        "hdr_job_id": hdr["job_id"],
        "hdr_status": hdr["status"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
