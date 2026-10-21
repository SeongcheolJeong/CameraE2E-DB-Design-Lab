#!/usr/bin/env python3
"""Prove separate Lens RAW calibration import and exact-RAW ISP in an installed Worker."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

import camera_platform
from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _artifact(transport: TestClient, descriptor: dict) -> bytes:
    response = transport.get(descriptor["url"])
    response.raise_for_status()
    content = response.content
    if _sha(content) != descriptor["sha256"] or len(content) != descriptor["bytes"]:
        raise RuntimeError("Registry artifact differs from its saved descriptor.")
    return content


def _numeric(shape: list[int]) -> dict:
    rows, columns = shape
    return {
        "schema": "camera_isp_calibration_v1",
        "sites": ["R", "Gr", "Gb", "B"],
        "lsc": {
            "schema": "native_four_site_grid_v1",
            "row_nodes": [0, rows - 1],
            "column_nodes": [0, columns - 1],
            "gains": [
                [[1.0, 1.1, 1.1, 1.2], [1.1, 1.2, 1.2, 1.3]],
                [[1.2, 1.3, 1.3, 1.4], [1.3, 1.4, 1.4, 1.5]],
            ],
            "valid_mask": [[[True] * 4 for _ in range(2)] for _ in range(2)],
            "interpolation": "bilinear",
            "normalization": "declared_per_site_reference",
            "reference_roi": {
                "row_start": 0,
                "row_end": rows,
                "column_start": 0,
                "column_end": columns,
            },
        },
        "noise": {
            "schema": "native_four_site_noise_v1",
            "slope_dn": [0.5] * 4,
            "intercept_dn2": [4.0] * 4,
            "signal_interval_dn": [-64, 4000],
            "reference": "black_subtracted_dn_before_lsc",
            "variance_model": "slope_times_max_signal_zero_plus_intercept",
            "spatial_independence": "assumed",
        },
    }


def verify(
    workspace: Path, installed_root: Path, state_dir: Path, proof: Path, output: Path
) -> dict:
    if not Path(camera_platform.__file__).resolve().is_relative_to(installed_root.resolve()):
        raise RuntimeError("Development package leaked into installed verification.")
    previous = json.loads(proof.read_text(encoding="utf-8"))
    if previous["version"] != camera_platform.__version__:
        raise RuntimeError("Lens source proof and installed package versions differ.")
    source_id = previous["records"]["lens_sensor_raw"]["id"]
    fixture = {
        "schema": "authored_flat_dark_numeric_fixture_v1",
        "source": "independent authored values for ISP wiring verification",
        "lsc_corner_gains": [1.0, 1.2, 1.3, 1.5],
        "noise_slope_dn": 0.5,
        "noise_intercept_dn2": 4.0,
        "physical_measurement": False,
    }
    fixture_bytes = json.dumps(fixture, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    fixture_path = output.with_name(output.stem + "-numeric-source.json")
    calibration_path = output.with_name(output.stem + "-calibration.json")
    for path in (output, fixture_path, calibration_path):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite installed proof input/output: {path}")
    app = create_app(state_dir, workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("A separate installed Worker did not start.")
        source = client.lens_sensor_raw(source_id)
        if source["status"] != "succeeded":
            raise RuntimeError("Pinned Lens RAW source is not complete.")
        original = np.load(io.BytesIO(_artifact(
            transport, source["result"]["summary"]["preview"]["raw_artifact"]
        )), allow_pickle=False)
        template = client.lens_sensor_calibrated_isp_template(
            source_lens_sensor_job_id=source_id
        )
        binding = template["expected_calibration_binding"]
        if binding["source_binding"]["source_lens_sensor_job_id"] != source_id:
            raise RuntimeError("Calibration target does not bind the selected Lens RAW.")
        fixture_path.write_bytes(fixture_bytes)
        record = {
            "schema": "camerae2e_lens_sensor_isp_calibration_v1",
            "provenance": {
                "source_kind": "simulated",
                "source_uri": "artifact://" + fixture_path.name,
                "method_and_version": "authored_numeric_fixture:1",
                "source_dataset_id": "independent-authored-flat-dark-values",
                "source_dataset_sha256": _sha(fixture_bytes),
                "target_raw_used_for_fitting": False,
                "uncertainty": {
                    "kind": "unknown",
                    "reason": "Fixture numbers are authored; physical fit is not established.",
                },
            },
            "binding": binding,
            "payload": _numeric(binding["geometry"]["full_sensor_shape"]),
        }
        calibration_path.write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        imported = client.import_lens_sensor_isp_calibration(calibration_path)
        spec = template["specification"]
        spec.update(
            name="installed Lens RAW declared calibration",
            source_lens_sensor_job_id=source_id,
            calibration_sha256=imported["calibration_sha256"],
            acknowledge_simulation_limits=True,
        )
        spec["profile"]["calibration_id"] = "sha256:" + imported["calibration_sha256"]
        spec["profile"]["spatial_lsc"]["mode"] = "native_four_site_grid_v1"
        spec["profile"]["raw_denoise"].update(
            mode="calibrated_same_site_filter_v1", strength=0.8
        )
        queued = client.submit_lens_sensor_calibrated_isp(spec)
        if transport.get(
            f"/api/v1/lens-sensor-calibrated-isp/{queued['id']}/export"
        ).status_code != 409:
            raise RuntimeError("Queued Lens calibrated ISP export was not withheld.")
        finished = client.wait(
            queued["id"], timeout=180, expected_kind="lens_sensor_calibrated_isp"
        )
        if finished["status"] != "succeeded":
            raise RuntimeError(f"Installed Lens ISP failed: {finished.get('error')}")
        summary = finished["result"]["summary"]
        with np.load(io.BytesIO(_artifact(transport, summary["evidence"]["npz"])),
                     allow_pickle=False) as arrays:
            preserved = np.array_equal(arrays["source_raw_dn_frame"], original)
            lsc = bool(np.max(arrays["candidate_lsc_gain_map"]) > 1)
            denoise = bool(np.max(arrays["candidate_noise_variance_hint_dn2"]) > 0)
        if (
            not preserved or not lsc or not denoise
            or summary["before_after"]["default_vs_candidate"][
                "quality_improvement_claimed"
            ] is not False
            or summary["source_binding"] != binding
        ):
            raise RuntimeError("Installed calibrated ISP lost RAW, binding or stage evidence.")
        return {
            "schema": "camera_platform_lens_calibrated_isp_installed_proof_v1",
            "version": camera_platform.__version__,
            "package_root": str(installed_root),
            "worker_child_pid": worker.pid,
            "source_lens_sensor_job_id": source_id,
            "job_id": finished["id"],
            "job_status": finished["status"],
            "numeric_source_sha256": _sha(fixture_bytes),
            "calibration_sha256": imported["calibration_sha256"],
            "source_raw_exact": preserved,
            "lsc_numeric_effect": lsc,
            "denoise_numeric_effect": denoise,
            "artifact_sha256": [item["sha256"] for item in finished["artifacts"]],
            "limits": {
                "calibration": "authored_simulated_fixture_not_measured_or_fitted",
                "independence": "declared_not_independently_verified",
                "lens_radiometry": "unknown_unity_assumption",
                "physical_validation": "not_established",
                "quality_improvement_claimed": False,
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--source-proof", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(
        args.workspace, args.installed_root, args.state_dir,
        args.source_proof, args.output,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"job_id": result["job_id"], "status": result["job_status"]}))


if __name__ == "__main__":
    main()
