#!/usr/bin/env python3
"""Prove dev8 joint Lens/Sensor jobs from an installed wheel and child Worker."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
from fastapi.testclient import TestClient

import camera_platform
import pyisetcam
from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _artifact(transport: TestClient, descriptor: dict) -> bytes:
    response = transport.get(descriptor["url"])
    response.raise_for_status()
    if _sha(response.content) != descriptor["sha256"]:
        raise RuntimeError("Installed artifact hash differs from the Registry descriptor.")
    return response.content


def _run(client: PlatformClient, kind: str, submit, spec: dict) -> dict:
    queued = submit(spec)
    finished = client.wait(queued["id"], timeout=600, expected_kind=kind)
    if finished["status"] != "succeeded":
        raise RuntimeError(f"{kind} {queued['id']} failed: {finished.get('error')}")
    return finished


def verify(workspace: Path, installed_root: Path, state_dir: Path) -> dict:
    installed_root = installed_root.resolve()
    for module in (camera_platform, pyisetcam):
        if not Path(module.__file__).resolve().is_relative_to(installed_root):
            raise RuntimeError(f"Development module leaked into installed proof: {module.__file__}")
    app = create_app(state_dir, workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("An independent installed Worker process did not start.")
        lens_template = client.lens_sensor_raw_template()
        lens = next(
            (row for row in lens_template["lens_catalog"]["revisions"]
             if row["preview_status"] == "ready"), None,
        )
        sensor = next(
            (row for row in lens_template["sensor_catalog"]
             if row["report_code"] == "DEF-2512-801"), None,
        )
        if lens is None or sensor is None:
            raise RuntimeError("Installed workspace lacks an eligible Lens/2x2 Bayer Sensor pair.")
        source_identities = {
            "lens_revision_id": lens["revision_id"],
            "sensor_revision": {"identifier": "DEF-2512-801", "run_id": sensor["run_id"]},
        }
        lens_spec = lens_template["specification"]
        lens_spec.update(
            name="installed joint source RAW", **source_identities,
            scene_kind="uniform", sensor_size=[8, 8], noise_flag=2,
            accept_approximate_lens_radiometry=True,
        )
        source = _run(client, "lens_sensor_raw", client.submit_lens_sensor_raw, lens_spec)
        preview = source["result"]["summary"]["preview"]
        source_raw = np.load(
            io.BytesIO(_artifact(transport, preview["raw_artifact"])), allow_pickle=False
        )
        if source_raw.dtype != np.int32 or source_raw.shape != (8, 8):
            raise RuntimeError("Installed joint source did not retain native int32 RAW.")

        isp_spec = client.lens_sensor_isp_replay_template()["specification"]
        isp_spec.update(
            name="installed joint source ISP", source_lens_sensor_job_id=source["id"],
            acknowledge_simulation_limits=True,
        )
        isp_spec["profile"]["white_balance"]["gains_rgb"] = [1.2, 1.0, 0.8]
        isp = _run(
            client, "lens_sensor_isp_replay", client.submit_lens_sensor_isp_replay, isp_spec
        )
        isp_summary = isp["result"]["summary"]
        with np.load(
            io.BytesIO(_artifact(transport, isp_summary["evidence"]["npz"])),
            allow_pickle=False,
        ) as arrays:
            if not np.array_equal(arrays["source_raw_dn_frame"], source_raw):
                raise RuntimeError("Installed ISP did not use exact Lens x Sensor source RAW.")
            if arrays["candidate_rgb"].shape != (8, 8, 3):
                raise RuntimeError("Installed ISP candidate RGB shape is wrong.")

        dataset_spec = client.retained_raw_dataset_template(
            source_kind="lens_sensor_raw"
        )["specification"]
        dataset_spec.update(
            name="installed joint source dataset", source_job_id=source["id"],
            acknowledge_simulation_limits=True,
        )
        dataset = _run(
            client, "retained_raw_dataset", client.submit_retained_raw_dataset, dataset_spec
        )
        bundle = dataset["result"]["summary"]["bundle"]
        with ZipFile(io.BytesIO(_artifact(transport, bundle))) as archive:
            raw_stream = io.BytesIO(archive.read("raw/retained_0000.npz"))
            with np.load(raw_stream, allow_pickle=False) as arrays:
                if not np.array_equal(arrays["raw"], source_raw):
                    raise RuntimeError("Installed dataset changed original Lens x Sensor RAW.")
        perception_spec = client.lens_sensor_perception_template()["specification"]
        perception_spec.update(
            name="installed joint source perception handoff",
            source_dataset_job_id=dataset["id"],
            acknowledge_simulation_limits=True,
        )
        perception = _run(
            client, "lens_sensor_perception", client.submit_lens_sensor_perception,
            perception_spec,
        )
        handoff = perception["result"]["summary"]
        if (
            handoff["perception_metric"]["available"] is not False
            or handoff["detector_evaluation_performed"] is not False
            or handoff["dataset_bundle"] != bundle
        ):
            raise RuntimeError("Installed perception handoff invented a score or lost its bundle.")

        hdr_spec = client.lens_sensor_hdr_template()["specification"]
        hdr_spec.update(
            name="installed joint source static HDR", **source_identities,
            sensor_size=[8, 8], accept_approximate_lens_radiometry=True,
        )
        hdr = _run(client, "lens_sensor_hdr", client.submit_lens_sensor_hdr, hdr_spec)
        hdr_summary = hdr["result"]["summary"]
        with np.load(
            io.BytesIO(_artifact(transport, hdr_summary["evidence"]["npz"])),
            allow_pickle=False,
        ) as arrays:
            hdr_raw = np.asarray(arrays["original_raw_dn"])
            if hdr_raw.dtype != np.int32 or hdr_raw.shape != (2, 8, 8):
                raise RuntimeError("Installed HDR did not retain two original RAW frames.")
            if _sha(hdr_raw.tobytes(order="C")) != hdr_summary["report"]["fusion"][
                "input_array_bytes_sha256"
            ]:
                raise RuntimeError("Installed HDR fusion input differs from retained RAW.")

        lfm_spec = client.lens_sensor_lfm_template()["specification"]
        lfm_spec.update(
            name="installed joint source PWM/LFM", **source_identities,
            accept_approximate_lens_radiometry=True,
            acknowledge_simulation_limits=True,
        )
        lfm = _run(client, "lens_sensor_lfm", client.submit_lens_sensor_lfm, lfm_spec)
        lfm_summary = lfm["result"]["summary"]
        with np.load(
            io.BytesIO(_artifact(transport, lfm_summary["report"]["artifacts"]["npz"])),
            allow_pickle=False,
        ) as arrays:
            lfm_raw = np.asarray(arrays["original_pwm_raw_dn"])
            if lfm_raw.dtype != np.int32 or lfm_raw.shape != (2, 4, 8, 8):
                raise RuntimeError("Installed LFM did not retain two-exposure/four-phase RAW.")
        if any(
            item["source_identities"] != source["result"]["summary"]["source_identities"]
            for item in (hdr_summary, lfm_summary)
        ):
            raise RuntimeError("Installed HDR/LFM used a different Lens x Sensor revision pair.")
        records = {
            name: {
                "id": item["id"], "status": item["status"],
                "artifact_sha256": [row["sha256"] for row in item["artifacts"]],
            }
            for name, item in (
                ("lens_sensor_raw", source), ("lens_sensor_isp_replay", isp),
                ("retained_raw_dataset", dataset), ("lens_sensor_perception", perception),
                ("lens_sensor_hdr", hdr), ("lens_sensor_lfm", lfm),
            )
        }
        return {
            "schema": "camera_platform_joint_installed_proof_v1",
            "version": camera_platform.__version__,
            "package_root": str(installed_root),
            "workspace": str(workspace),
            "state_dir": str(state_dir),
            "worker_child_pid": worker.pid,
            "selected_sources": source_identities,
            "records": records,
            "raw_evidence": {
                "source_shape": list(source_raw.shape),
                "isp_source_raw_exact": True,
                "dataset_source_raw_exact": True,
                "perception_metric_unavailable_without_truth": True,
                "hdr_original_raw_shape": list(hdr_raw.shape),
                "lfm_original_pwm_raw_shape": list(lfm_raw.shape),
            },
            "boundaries": {
                "lens_radiometry": "unknown_unity_assumption",
                "hdr_fpn": "zero_override_not_same_as_lfm_saved_fpn",
                "lfm_timing": "authored_not_measured",
                "isp": "actual_2x2_native_bayer_only_uncalibrated_v2",
                "perception": "raw_handoff_no_detector_or_ground_truth",
                "physical_validation": "not_established",
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.workspace, args.installed_root, args.state_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"version": result["version"], "jobs": len(result["records"]),
                      "output": str(args.output)}, sort_keys=True))


if __name__ == "__main__":
    main()
