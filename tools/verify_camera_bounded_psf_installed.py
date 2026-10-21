#!/usr/bin/env python3
"""Verify bounded Lens × Sensor RAW and exact dataset export from a wheel."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
from pathlib import Path
from zipfile import ZipFile

import numpy as np
from fastapi.testclient import TestClient

import camera_platform
import pyisetcam
from camera_platform.api import create_app
from camera_platform.registry import canonical
from camera_platform.sdk import PlatformClient
from pyisetcam.dataset import _sha256_array


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _verify_wheel(installed: Path, wheel: Path) -> dict:
    root = installed.resolve()
    wheel_version = wheel.name.split("-")[1]
    if camera_platform.__version__ != wheel_version or pyisetcam.__version__ != wheel_version:
        raise RuntimeError("Unexpected installed platform version.")
    for module in (camera_platform, pyisetcam):
        if not Path(module.__file__).resolve().is_relative_to(root):
            raise RuntimeError(f"{module.__name__} leaked from the source tree.")
    with ZipFile(wheel) as archive:
        members = [
            member for member in archive.infolist()
            if not member.is_dir() and not member.filename.endswith(".dist-info/RECORD")
        ]
        for member in members:
            if (root / member.filename).read_bytes() != archive.read(member):
                raise RuntimeError(f"Installed bytes differ from wheel: {member.filename}")
    return {
        "version": camera_platform.__version__,
        "wheel_sha256": _sha(wheel.read_bytes()),
        "installed_files_matching_wheel": len(members),
        "camera_platform_path": str(Path(camera_platform.__file__).resolve()),
        "pyisetcam_path": str(Path(pyisetcam.__file__).resolve()),
    }


def _client(transport: TestClient) -> PlatformClient:
    client = object.__new__(PlatformClient)
    client.http = transport
    session = transport.get("/api/v1/session")
    session.raise_for_status()
    transport.headers["X-Platform-Token"] = session.json()["token"]
    return client


def _artifact(transport: TestClient, descriptor: dict) -> bytes:
    response = transport.get(descriptor["url"])
    response.raise_for_status()
    content = response.content
    if _sha(content) != descriptor["sha256"] or len(content) != descriptor["bytes"]:
        raise RuntimeError("Downloaded artifact differs from its descriptor.")
    return content


def _raw_job(
    client: PlatformClient, transport: TestClient, *, lens: str,
    sensor_size: list[int], expected_raw_sha256: str,
) -> dict:
    template = client.lens_sensor_raw_template(scope="central_bounded")
    if template["specification"]["schema"] != "camerae2e_lens_sensor_raw_preview_study_v3":
        raise RuntimeError("Installed bounded template does not expose v3.")
    sensor = next(
        row for row in template["sensor_catalog"] if row["report_code"] == "DEF-2512-801"
    )
    spec = template["specification"]
    spec.update(
        lens_revision_id=lens,
        sensor_revision={"identifier": sensor["report_code"], "run_id": sensor["run_id"]},
        sensor_size=sensor_size,
        accept_approximate_lens_radiometry=True,
    )
    started = time.monotonic()
    queued = client.submit_lens_sensor_raw(spec)
    if queued["resource_preflight"]["scope"] != "central_bounded_strict_raytrace":
        raise RuntimeError("Bounded resource preflight was not frozen at submission.")
    finished = client.wait(queued["id"], expected_kind="lens_sensor_raw", timeout=180)
    elapsed = time.monotonic() - started
    if finished["status"] != "succeeded":
        raise RuntimeError(f"Installed bounded RAW job failed: {finished.get('error')}")
    detail = client.lens_sensor_raw(finished["id"])
    if detail["result"]["summary"] != finished["result"]["summary"]:
        raise RuntimeError("Stored bounded job changed on re-read.")
    summary = detail["result"]["summary"]
    preview = summary["preview"]
    psf = preview["lens_model"]["psf_execution"]
    if (
        summary["resource_preflight"] != preview["resource_preflight"]
        or psf["mode"] != "strict_bounded_streaming"
        or psf["complete_precomputed_stack"] is not False
        or psf["peak_cached_kernels"] > 4
        or psf.get("convolution_strategy") != "cropped_input_full_fft_same_output"
        or preview["physical_validation"] != "not_established"
    ):
        raise RuntimeError("Stored bounded job lacks strict execution evidence.")
    original = np.load(
        io.BytesIO(_artifact(transport, preview["raw_artifact"])), allow_pickle=False
    )
    raw_hash = _sha256_array(original)
    if (
        original.dtype != np.dtype("int32")
        or list(original.shape) != sensor_size
        or original.tolist() != preview["raw"]["values"]
        or raw_hash != expected_raw_sha256
    ):
        raise RuntimeError("Installed bounded RAW differs from source proof or saved artifact.")
    return {
        "job_id": finished["id"], "status": finished["status"],
        "lens_revision_id": lens, "sensor_revision": preview["sensor_revision"],
        "sensor_size": sensor_size,
        "elapsed_s": elapsed, "raw_content_sha256": raw_hash,
        "raw_artifact_sha256": preview["raw_artifact"]["sha256"],
        "resource_preflight": summary["resource_preflight"],
        "psf_execution": psf,
        "original_raw_exact": True,
        "source_numeric_proof_equal": True,
        "physical_validation": preview["physical_validation"],
    }


def _dataset(client: PlatformClient, transport: TestClient, source: dict) -> dict:
    template = client.retained_raw_dataset_template(source_kind="lens_sensor_raw")
    spec = template["specification"]
    spec.update(
        source_job_id=source["job_id"],
        split="train", labels="unlabeled", acknowledge_simulation_limits=True,
    )
    queued = client.submit_retained_raw_dataset(spec)
    finished = client.wait(queued["id"], expected_kind="retained_raw_dataset", timeout=60)
    if finished["status"] != "succeeded":
        raise RuntimeError(f"Bounded RAW dataset handoff failed: {finished.get('error')}")
    summary = finished["result"]["summary"]
    if summary["source_binding"]["source_lens_sensor_job_id"] != source["job_id"]:
        raise RuntimeError("Dataset source binding differs from bounded RAW job.")
    return {
        "job_id": finished["id"], "status": finished["status"],
        "source_job_id": source["job_id"],
        "source_raw_content_sha256": summary["source_binding"]["source_raw_content_sha256"],
        "labels": "unlabeled", "physical_validation": "not_established",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--proof-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.state.exists():
        raise RuntimeError("A fresh isolated state path is required.")
    package = _verify_wheel(args.installed_root, args.wheel)
    app = create_app(args.state, args.workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed child Worker did not start.")
        results = []
        prior = json.loads(
            (args.proof_dir / "bounded-psf-sparse-source-dev13.json").read_text()
        )
        for case in prior["cases"]:
            results.append(_raw_job(
                client, transport, lens=case["lens_revision_id"],
                sensor_size=case["sensor_size"],
                expected_raw_sha256=case["raw_content_sha256"],
            ))
        dataset = _dataset(client, transport, results[0])
        proof = {
            "schema": "camerae2e_installed_bounded_psf_proof_v1",
            "package": package,
            "worker_child_pid": worker.pid,
            "jobs": results,
            "retained_dataset_handoff": dataset,
            "legacy_p0014_precomputed_job": "timed_out_over_120s_in_prior_dev9_proof",
            "physical_validation": "not_established",
            "product_readiness": "not_established",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output), "version": package["version"],
        "job_statuses": [item["status"] for item in results],
        "dataset_status": dataset["status"],
        "elapsed_s": [round(item["elapsed_s"], 2) for item in results],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
