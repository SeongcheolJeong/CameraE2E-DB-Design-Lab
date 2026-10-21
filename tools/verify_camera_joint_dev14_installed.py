#!/usr/bin/env python3
"""Exercise the dev14 joint workflows using only an installed wheel and child Worker."""

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
from verify_camera_bounded_psf_installed import _artifact, _client, _verify_wheel

from camera_platform.api import create_app
from camera_platform.registry import canonical
from pyisetcam.dataset import _sha256_array


def _finished(client, queued: dict, kind: str, timeout: int = 180) -> dict:
    finished = client.wait(queued["id"], expected_kind=kind, timeout=timeout)
    if finished["status"] != "succeeded":
        raise RuntimeError(f"{kind} failed: {finished.get('error')}")
    return finished


def _matrix(client, transport: TestClient, source_cases: list[dict]) -> tuple[dict, dict]:
    template = client.lens_sensor_raw_template(scope="central_bounded")
    if template["specification"]["schema"] != "camerae2e_lens_sensor_raw_preview_study_v3":
        raise RuntimeError("Installed RAW template is not bounded v3.")
    sensors = {(row["report_code"], row["run_id"]): row for row in template["sensor_catalog"]}
    entries = []
    original = {}
    for case in source_cases:
        if (case["sensor_report_code"], case["sensor_run_id"]) not in sensors:
            raise RuntimeError("Pinned sensor run is missing from the installed catalog.")
        spec = client.lens_sensor_raw_template(scope="central_bounded")["specification"]
        spec.update(
            lens_revision_id=case["lens_revision_id"],
            sensor_revision={
                "identifier": case["sensor_report_code"],
                "run_id": case["sensor_run_id"],
            },
            sensor_size=[32, 32],
            accept_approximate_lens_radiometry=True,
        )
        started = time.monotonic()
        queued = client.submit_lens_sensor_raw(spec)
        if queued["resource_preflight"]["scope"] != "central_bounded_strict_raytrace":
            raise RuntimeError("RAW resource admission was not frozen.")
        finished = _finished(client, queued, "lens_sensor_raw")
        detail = client.lens_sensor_raw(finished["id"])
        summary = detail["result"]["summary"]
        preview = summary["preview"]
        psf = preview["lens_model"]["psf_execution"]
        raw = np.load(io.BytesIO(_artifact(transport, preview["raw_artifact"])), allow_pickle=False)
        raw_hash = _sha256_array(raw)
        if (
            raw.dtype != np.dtype("int32")
            or raw.shape != (32, 32)
            or raw_hash != case["raw_content_sha256"]
            or psf["mode"] != "strict_bounded_streaming"
            or psf["complete_precomputed_stack"] is not False
            or summary["resource_preflight"] != preview["resource_preflight"]
        ):
            raise RuntimeError(
                "Installed RAW differs from the source proof or strict PSF contract."
            )
        slot = ("L1" if case["lens"] == "P0014" else "L2") + (
            "S1" if case["sensor_report_code"] == "DEF-2512-801" else "S2"
        )
        original[slot] = raw
        entries.append({
            "slot": slot,
            "job_id": finished["id"],
            "lens_revision_id": case["lens_revision_id"],
            "sensor_report_code": case["sensor_report_code"],
            "sensor_run_id": case["sensor_run_id"],
            "elapsed_s": time.monotonic() - started,
            "raw_content_sha256": raw_hash,
            "raw_artifact_sha256": preview["raw_artifact"]["sha256"],
            "horizontal_fov_deg": preview["scene"]["horizontal_fov_deg"],
            "psf_mode": psf["mode"],
            "physical_validation": preview["physical_validation"],
        })
    if {item["slot"] for item in entries} != {"L1S1", "L1S2", "L2S1", "L2S2"}:
        raise RuntimeError("Four RAW captures do not form a 2 x 2 matrix.")
    return {"cases": entries}, original


def _quality(client, transport: TestClient, matrix: dict, originals: dict) -> dict:
    template = client.lens_sensor_quality_template()
    spec = template["specification"]
    if spec["schema"] != "camerae2e_lens_sensor_quality_study_v1":
        raise RuntimeError("Installed 2 x 2 quality contract is missing.")
    spec.update(
        source_jobs={item["slot"]: item["job_id"] for item in matrix["cases"]},
        acknowledge_simulation_limits=True,
    )
    finished = _finished(client, client.submit_lens_sensor_quality(spec),
                         "lens_sensor_quality_study")
    detail = client.lens_sensor_quality(finished["id"])
    summary = detail["result"]["summary"]
    comparison = summary["comparison"]
    if (
        set(summary["cases"]) != set(originals)
        or comparison["matched_source_controls"] is not True
        or comparison["matched_horizontal_fov"] is not False
        or comparison["edge_mtf50_diagnostic_delta_allowed"] is not False
        or comparison["temporal_snr_delta_allowed"] is not False
        or comparison["product_ranking_allowed"] is not False
    ):
        raise RuntimeError("Quality study overclaimed incomparable 2 x 2 sources.")
    with np.load(io.BytesIO(_artifact(transport, summary["evidence_arrays"])),
                 allow_pickle=False) as arrays:
        for slot, expected in originals.items():
            if not np.array_equal(arrays[f"{slot}_raw_dn"], expected):
                raise RuntimeError(f"Quality study altered {slot} original RAW.")
    return {
        "job_id": finished["id"],
        "case_count": len(summary["cases"]),
        "edge_mtf50": {slot: case["edge_mtf50"] for slot, case in summary["cases"].items()},
        "comparison": comparison,
        "original_raw_exact": True,
    }


def _bounded_temporal(client, transport: TestClient, *, kind: str, case: dict) -> dict:
    if kind == "hdr":
        template = client.lens_sensor_hdr_template(scope="central_bounded")
        submit, detail, expected_schema = (
            client.submit_lens_sensor_hdr, client.lens_sensor_hdr,
            "camerae2e_lens_sensor_static_hdr_study_v2",
        )
        raw_key, raw_shape = "original_raw_dn", (2, 8, 8)
    else:
        template = client.lens_sensor_lfm_template(scope="central_bounded")
        submit, detail, expected_schema = (
            client.submit_lens_sensor_lfm, client.lens_sensor_lfm,
            "camerae2e_lens_sensor_pwm_lfm_study_v2",
        )
        raw_key, raw_shape = "original_pwm_raw_dn", (2, 4, 8, 8)
    spec = template["specification"]
    if spec["schema"] != expected_schema:
        raise RuntimeError(f"Installed bounded {kind} template is missing.")
    spec.update(
        lens_revision_id=case["lens_revision_id"],
        sensor_revision={"identifier": case["sensor_report_code"],
                         "run_id": case["sensor_run_id"]},
        sensor_size=[8, 8],
        accept_approximate_lens_radiometry=True,
    )
    if kind == "lfm":
        spec["acknowledge_simulation_limits"] = True
    started = time.monotonic()
    queued = submit(spec)
    if queued["resource_preflight"]["scope"] != "central_bounded_strict_raytrace":
        raise RuntimeError(f"{kind} resource admission was not frozen.")
    finished = _finished(client, queued, "lens_sensor_" + kind)
    summary = detail(finished["id"])["result"]["summary"]
    report = summary["report"]
    if (
        summary["resource_preflight"] != report["resource_preflight"]
        or report["psf_execution"]["mode"] != "strict_bounded_streaming"
        or report["psf_execution"]["complete_precomputed_stack"] is not False
    ):
        raise RuntimeError(f"Bounded {kind} lost frozen PSF execution evidence.")
    descriptor = (summary["evidence"]["npz"] if kind == "hdr"
                  else report["artifacts"]["npz"])
    with np.load(io.BytesIO(_artifact(transport, descriptor)), allow_pickle=False) as arrays:
        raw = np.asarray(arrays[raw_key])
    if raw.dtype != np.dtype("int32") or raw.shape != raw_shape:
        raise RuntimeError(f"Bounded {kind} lost original integer RAW streams.")
    return {
        "job_id": finished["id"], "status": finished["status"],
        "schema": summary["schema"], "elapsed_s": time.monotonic() - started,
        "raw_shape": list(raw.shape), "raw_bytes_sha256": hashlib.sha256(raw.tobytes()).hexdigest(),
        "resource_preflight": summary["resource_preflight"],
        "psf_mode": report["psf_execution"]["mode"],
        "fusion_valid_pixels": report["fusion"]["valid_pixels"] if kind == "hdr" else None,
        "physical_validation": "not_established",
    }


def _labeled_dataset(client, transport: TestClient, source: dict, raw: np.ndarray) -> dict:
    template = client.retained_raw_dataset_template(
        source_kind="lens_sensor_raw_labeled", source_job_id=source["job_id"]
    )
    spec = template["specification"]
    annotation = template["annotation_template"]
    if (
        spec["schema"] != "camerae2e_retained_raw_dataset_study_v3"
        or annotation["source_binding"]["source_raw_content_sha256"]
        != source["raw_content_sha256"]
        or annotation["raw_shape"] != [32, 32]
    ):
        raise RuntimeError("Installed labeled dataset template lost source binding.")
    annotation["provenance"].update(
        provider_id="dev14-installed-proof", annotation_set_id="synthetic-edge-1",
        method="synthetic_author", coverage="partial",
    )
    annotation["objects"] = [{"label": "edge", "bbox_xyxy": [2, 2, 16, 16]}]
    imported = client.import_retained_raw_annotation(annotation)
    if imported["computed_sha256"] != imported["artifact"]["sha256"]:
        raise RuntimeError("Server annotation canonical hash differs from retained artifact.")
    spec.update(
        source_job_id=source["job_id"], split="train",
        annotation_artifact_sha256=imported["artifact"]["sha256"],
        acknowledge_simulation_limits=True,
    )
    finished = _finished(client, client.submit_retained_raw_dataset(spec),
                         "retained_raw_dataset")
    summary = client.retained_raw_dataset(finished["id"])["result"]["summary"]
    if (
        summary["labels"] != "user_provided_unverified"
        or summary["raw_values_byte_equal"] is not True
        or summary["dataset_validation"]["ok"] is not True
        or summary["annotation_binding"]["annotation_artifact_sha256"]
        != imported["artifact"]["sha256"]
    ):
        raise RuntimeError("Labeled dataset export lacks exact RAW or annotation evidence.")
    with ZipFile(io.BytesIO(_artifact(transport, summary["bundle"]))) as archive:
        with np.load(io.BytesIO(archive.read("raw/retained_0000.npz")),
                     allow_pickle=False) as arrays:
            retained = arrays["raw"]
        label = json.loads(archive.read("labels/retained_0000.json"))
        manifest = json.loads(archive.read("manifest.json"))
        source_annotation = archive.read("annotations/retained_0000-source.json")
    if (
        not np.array_equal(retained, raw)
        or retained.dtype != np.dtype("int32")
        or label["labels"]["objects"][0]["label"] != "edge"
        or manifest["split_policy"]["holdout_independence_claimed"] is not False
        or hashlib.sha256(source_annotation).hexdigest() != imported["artifact"]["sha256"]
    ):
        raise RuntimeError("Labeled dataset ZIP changed source RAW or label binding.")
    return {
        "job_id": finished["id"], "status": finished["status"],
        "annotation_sha256": imported["artifact"]["sha256"],
        "bundle_sha256": summary["bundle"]["sha256"],
        "source_job_id": source["job_id"], "original_raw_exact": True,
        "labels": summary["labels"], "holdout_independence_claimed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--source-proof", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.state.exists():
        raise RuntimeError("A fresh isolated installed state path is required.")
    prior = json.loads(args.source_proof.read_text(encoding="utf-8"))
    package = _verify_wheel(args.installed_root, args.wheel)
    app = create_app(args.state, args.workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed child Worker did not start.")
        matrix, originals = _matrix(client, transport, prior["cases"])
        quality = _quality(client, transport, matrix, originals)
        hdr = _bounded_temporal(client, transport, kind="hdr", case=prior["cases"][0])
        lfm = _bounded_temporal(client, transport, kind="lfm", case=prior["cases"][2])
        source = next(item for item in matrix["cases"] if item["slot"] == "L1S1")
        dataset = _labeled_dataset(client, transport, source, originals["L1S1"])
        proof = {
            "schema": "camerae2e_installed_joint_dev14_proof_v1",
            "package": package,
            "worker_child_pid": worker.pid,
            "matrix": matrix,
            "quality": quality,
            "bounded_hdr": hdr,
            "bounded_lfm": lfm,
            "labeled_dataset": dataset,
            "physical_validation": "not_established",
            "product_readiness": "not_established",
            "independent_detector_inference": "not_performed",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output), "version": package["version"],
        "raw_cases": len(matrix["cases"]),
        "quality_case_count": quality["case_count"],
        "quality_pairwise_delta_allowed": quality["comparison"][
            "edge_mtf50_diagnostic_delta_allowed"
        ],
        "hdr": hdr["status"], "lfm": lfm["status"], "dataset": dataset["status"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
