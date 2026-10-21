#!/usr/bin/env python3
"""Check the installed v3 RAW handoff to ISP, supplied-box evaluation and cohort."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
from fastapi.testclient import TestClient
from verify_camera_bounded_psf_installed import _artifact, _client, _verify_wheel
from verify_camera_joint_dev11_installed import _perception_one

from camera_platform.api import create_app
from camera_platform.registry import canonical


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--source-proof", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not (args.state / "registry.sqlite").is_file():
        raise RuntimeError("The installed v3 source-job state is missing.")
    source_proof = json.loads(args.source_proof.read_text(encoding="utf-8"))
    if source_proof["schema"] != "camerae2e_installed_bounded_psf_proof_v1":
        raise RuntimeError("Unexpected v3 source proof.")
    package = _verify_wheel(args.installed_root, args.wheel)
    ids = [item["job_id"] for item in source_proof["jobs"][:2]]
    app = create_app(args.state, args.workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed child Worker did not start.")
        sources = [client.lens_sensor_raw(identity) for identity in ids]
        if any(item["status"] != "succeeded" for item in sources):
            raise RuntimeError("A bounded source job is no longer complete.")

        dataset = client.retained_raw_dataset(
            source_proof["retained_dataset_handoff"]["job_id"]
        )
        ds_summary = dataset["result"]["summary"]
        if (
            ds_summary["raw_values_byte_equal"] is not True
            or not ds_summary["dataset_validation"]["ok"]
        ):
            raise RuntimeError("Bounded RAW dataset export did not validate exact data.")
        source_raw_descriptor = sources[0]["result"]["summary"]["preview"]["raw_artifact"]
        source_raw = np.load(
            io.BytesIO(_artifact(transport, source_raw_descriptor)),
            allow_pickle=False,
        )
        with ZipFile(io.BytesIO(_artifact(transport, ds_summary["bundle"]))) as archive:
            with np.load(
                io.BytesIO(archive.read("raw/retained_0000.npz")), allow_pickle=False
            ) as arrays:
                retained = arrays["raw"]
        if retained.dtype != np.dtype("int32") or retained.tobytes() != source_raw.tobytes():
            raise RuntimeError("Retained dataset changed the bounded source RAW bytes.")

        isp_template = client.lens_sensor_isp_replay_template()
        if not any(item["id"] == ids[0] for item in isp_template["source_lens_sensor_jobs"]):
            raise RuntimeError("Bounded source is absent from ISP source picker.")
        isp_spec = isp_template["specification"]
        isp_spec.update(source_lens_sensor_job_id=ids[0], acknowledge_simulation_limits=True)
        isp_queued = client.submit_lens_sensor_isp_replay(isp_spec)
        isp = client.wait(isp_queued["id"], expected_kind="lens_sensor_isp_replay", timeout=120)
        if isp["status"] != "succeeded":
            raise RuntimeError(f"Bounded RAW ISP replay failed: {isp.get('error')}")
        isp_summary = isp["result"]["summary"]
        if (
            isp_summary["source_binding"]["source_lens_sensor_job_id"] != ids[0]
            or isp_summary["raw_values_byte_equal"] is not True
        ):
            raise RuntimeError("ISP replay lost exact bounded source RAW identity.")

        perception = _perception_one(client, ids[0], isp["id"])

        cohort_template = client.lens_sensor_raw_cohort_template()
        if not {item["id"] for item in cohort_template["source_jobs"]}.issuperset(ids):
            raise RuntimeError("Bounded sources are absent from cohort source picker.")
        cohort_spec = cohort_template["specification"]
        cohort_spec.update(
            source_jobs=[
                {"source_job_id": ids[0], "split": "train"},
                {"source_job_id": ids[1], "split": "test"},
            ],
            acknowledge_simulation_limits=True,
        )
        cohort_queued = client.submit_lens_sensor_raw_cohort(cohort_spec)
        cohort = client.wait(
            cohort_queued["id"], expected_kind="lens_sensor_raw_cohort", timeout=120
        )
        if cohort["status"] != "succeeded":
            raise RuntimeError(f"Bounded RAW cohort failed: {cohort.get('error')}")
        cohort_summary = cohort["result"]["summary"]
        if cohort_summary["case_count"] != 2 or not cohort_summary["dataset_validation"]["ok"]:
            raise RuntimeError("Bounded source cohort lacks two validated RAW records.")
        with ZipFile(io.BytesIO(_artifact(transport, cohort_summary["bundle"]))) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        if {item["split"] for item in manifest["records"]} != {"train", "test"}:
            raise RuntimeError("Cohort did not retain the declared per-source splits.")
        proof = {
            "schema": "camerae2e_installed_bounded_downstream_proof_v1",
            "package": package,
            "worker_child_pid": worker.pid,
            "source_job_ids": ids,
            "dataset": {
                "job_id": dataset["id"], "raw_bytes_equal": True,
                "bundle_sha256": ds_summary["bundle"]["sha256"],
            },
            "isp": {
                "job_id": isp["id"], "status": isp["status"],
                "source_raw_exact": True,
            },
            "perception": perception,
            "cohort": {
                "job_id": cohort["id"], "status": cohort["status"],
                "case_count": cohort_summary["case_count"],
                "bundle_sha256": cohort_summary["bundle"]["sha256"],
                "split_policy": manifest["split_policy"],
            },
            "physical_validation": "not_established",
            "independent_detector_inference": "not_performed",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output), "isp": isp["status"],
        "perception": perception["status"], "cohort": cohort["status"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
